"""API keys and the caller's tenant (plan V2-10; docs/ARCHITECTURE_V2.md §12).

Every ``/v1`` route calls ``get_current_tenant``. The Bearer key is hashed (SHA-256) and
looked up among the stored keys; only an active (not revoked) key is accepted. Anything
else is ``401``. The raw key is never stored or logged.

The development key (``relay-dev-key``, the web UI's default) maps to the development
tenant only when it is enabled: by default it is enabled when the server listens on a
loopback address (one person on their own machine), and disabled otherwise
(``[auth] dev_key_enabled``).

Each key may have a daily request limit (``api_key.daily_limit``); ``charge`` counts the
key's ``/v1/ask`` requests for the current UTC day in memory (a restart starts the count
again) and refuses with ``429`` past the limit.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Final

from fastapi import Request

from relay.app.bootstrap import DEV_TENANT_ID
from relay.app.routes.rejected import Rejected
from relay.store import Store
from relay.store.repositories import KEY_PREFIX, hash_api_key

DEV_KEY: Final = "relay-dev-key"
ROLE_ADMIN: Final = "admin"


@dataclass(frozen=True)
class TenantContext:
    """Who is calling: the tenant, and the key it used."""

    tenant_id: str
    key_id: str | None  # None: the development key
    role: str
    daily_limit: int | None = None
    dev: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def masked_key(self) -> str:
        return "development key" if self.dev else f"{KEY_PREFIX}… (id {self.key_id})"


def _today() -> date:
    return datetime.now(UTC).date()


def bearer_key(request: Request) -> str:
    scheme, _, key = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not key.strip():
        raise Rejected(401, "unauthorized", "an API key is required: Authorization: Bearer <key>")
    return key.strip()


@dataclass
class Authenticator:
    store: Store
    dev_key: str | None = DEV_KEY  # None: the development key is not accepted
    today: Callable[[], date] = _today
    _used: dict[str, int] = field(default_factory=dict, repr=False)
    _day: date | None = field(default=None, repr=False)

    async def authenticate(self, request: Request) -> TenantContext:
        key = bearer_key(request)
        if self.dev_key is not None and hmac.compare_digest(key.encode(), self.dev_key.encode()):
            return TenantContext(DEV_TENANT_ID, None, ROLE_ADMIN, dev=True)
        record = await self.store.api_keys.get_by_hash(hash_api_key(key))
        if record is None or record.revoked:  # unknown and revoked look the same
            raise Rejected(401, "unauthorized", "the API key is not valid")
        return TenantContext(record.tenant_id, record.id, record.role, record.daily_limit)

    def charge(self, caller: TenantContext) -> None:
        """Count one ``/v1/ask`` request against the key's daily limit (429 past it)."""
        if caller.key_id is None or caller.daily_limit is None:
            return
        today = self.today()
        if today != self._day:
            self._day, self._used = today, {}
        used = self._used.get(caller.key_id, 0)
        if used >= caller.daily_limit:
            raise Rejected(429, "daily_limit_reached",
                           f"this API key's daily limit of {caller.daily_limit} requests is "
                           "used up; it resets at 00:00 UTC")
        self._used[caller.key_id] = used + 1


async def get_current_tenant(request: Request, auth: Authenticator) -> TenantContext:
    """The caller's tenant, or ``Rejected(401)``."""
    return await auth.authenticate(request)


def require_admin(caller: TenantContext) -> None:
    if not caller.is_admin:
        raise Rejected(403, "forbidden", "this action needs an admin API key")


__all__ = ["DEV_KEY", "ROLE_ADMIN", "Authenticator", "TenantContext", "bearer_key",
           "get_current_tenant", "require_admin"]
