"""``GET /v1/requests/{request_id}``: one request's summary, latency waterfall, tokens,
verifier breakdown and trace (plan V2-11; docs/ARCHITECTURE_V2.md §9, §11).

Only the tenant that made the request can read it; any other caller gets ``404`` (the
request's existence is not revealed). The data is the request-log row written when the
request ended (the log is written in the background, so a request that has just ended can
take a moment to appear). The question is shown only as its SHA-256.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from relay.api.auth import Authenticator
from relay.app.routes.rejected import Rejected
from relay.store import Store
from relay.store.repositories import RequestLogEntry

logger = logging.getLogger(__name__)

_EMPTY_WATERFALL = {"rewrite_ms": 0.0, "queue_ms": 0.0, "retrieval_ms": 0.0,
                    "generation_ms": 0.0, "verification_ms": 0.0}


def _error(status: int, code: str, message: str) -> JSONResponse:
    headers = {"www-authenticate": "Bearer"} if status == 401 else None
    return JSONResponse({"error": code, "message": message}, status_code=status,
                        headers=headers)


def request_view(entry: RequestLogEntry) -> dict[str, Any]:
    """The JSON for one request-log row."""
    breakdown: dict[str, Any] = {}
    if entry.breakdown:
        try:
            breakdown = json.loads(entry.breakdown)
        except json.JSONDecodeError:
            logger.warning("request %s: unreadable breakdown; served without it",
                           entry.request_id)
    waterfall = {**_EMPTY_WATERFALL, **(breakdown.get("waterfall") or {})}
    waterfall.setdefault("total_ms", entry.total_ms)
    verifier = breakdown.get("verifier") or {}
    return {
        "request_id": entry.request_id, "question_hash": entry.question_hash,
        "status": entry.status, "verdict": entry.verdict, "lane": entry.lane_name,
        "cost_usd": entry.cost_usd, "escalated": entry.escalated,
        "collection_id": entry.collection_id, "created_at": entry.created_at,
        "cache_status": breakdown.get("cache_status"),
        "coalesced": bool(breakdown.get("coalesced", entry.coalesced)),
        "first_sentence_ms": entry.ttft_ms,
        "waterfall": waterfall,
        "tokens": {"in": entry.tokens_in, "out": entry.tokens_out},
        "verifier": {"sentences_checked": int(verifier.get("sentences_checked", 0)),
                     "sentences_passed": int(verifier.get("sentences_passed", 0)),
                     "partial": bool(verifier.get("partial", False))},
        "trace": breakdown.get("spans", []),
    }


def requests_router(store: Store, auth: Authenticator) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.get("/requests/{request_id}")
    async def get_request(request_id: str, request: Request) -> JSONResponse:
        try:
            caller = await auth.authenticate(request)
            entry = await store.request_log.get_by_request_id(request_id)
            if entry is None or entry.tenant_id != caller.tenant_id:
                raise Rejected(404, "request_not_found", "no such request")
        except Rejected as exc:
            return _error(exc.status, exc.code, str(exc))
        return JSONResponse(request_view(entry))

    return router


__all__ = ["request_view", "requests_router"]
