"""Typed service settings for ``relay serve`` (docs/ARCHITECTURE_V2.md §13; plan V2-01).

Settings come from three layers, later layers winning:

1. built-in defaults (they match ``relay.example.toml`` and work with the stand-in);
2. a TOML file, normally ``relay.toml``;
3. environment variables ``RELAY_<SECTION>__<KEY>``, for example ``RELAY_SERVER__PORT=9000``
   or ``RELAY_CACHE__MODE=off``. Values are parsed as JSON when they can be
   (numbers, ``true``/``false``, lists), otherwise taken as strings.

**Secrets never live here.** ``GROQ_API_KEY`` and ``MAX_REAL_CALLS`` stay in the
environment / ``.env`` and are read by ``relay.config.load_settings``. A key-like field in
the TOML file is rejected, because unknown fields are errors.

Lists of tables (``[[lanes]]``) cannot be overridden from the environment; edit the file.

``RELAY_*`` overrides are read from the process environment only, not from ``.env``
(which holds secrets). Relative paths resolve against the working directory, so run the
service from the repository root or use absolute paths.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from relay.errors import ConfigurationError
from relay.retrieval.hybrid_index import RETRIEVAL_SETTINGS

ENV_PREFIX: Final = "RELAY_"
ENV_DELIMITER: Final = "__"
DEFAULT_CONFIG_FILE: Final = Path("relay.toml")
CALIBRATED_PROFILE: Final = Path(
    "evidence/standin_calibration/cal-20261113T022403Z-3f687c6/profile.json")

_MODEL = ConfigDict(frozen=True, extra="forbid")
Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$", max_length=64)]
PositiveInt = Annotated[int, Field(ge=1)]
CacheMode = Literal["off", "memory", "sqlite"]


class ServerSettings(BaseModel):
    model_config = _MODEL

    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 8000


class LaneSettings(BaseModel):
    """One (provider, model) pair. V2-01 serves from the first lane only."""

    model_config = _MODEL

    name: Slug
    kind: Literal["standin", "groq"]
    model: Annotated[str, Field(min_length=1, max_length=128)]
    # stand-in: URL of a running stand-in (ignored when embedded); groq: optional override
    # of the Groq base URL from the environment settings.
    base_url: str | None = None
    # stand-in only: start the stand-in inside the same process on 127.0.0.1.
    embedded: bool = False
    # stand-in only: a profile.json from a calibration run; None = uncalibrated defaults.
    profile: Path | None = None
    max_completion_tokens: PositiveInt = 512
    reasoning_effort: str | None = None

    @model_validator(mode="after")
    def _check(self) -> LaneSettings:
        if self.kind == "groq" and (self.embedded or self.profile is not None):
            raise ValueError("embedded and profile apply to stand-in lanes only")
        if self.kind == "standin" and not self.embedded and not self.base_url:
            raise ValueError("a stand-in lane needs embedded = true or a base_url")
        return self


class GuardSettings(BaseModel):
    """ProviderGuard limits for the lane. ``None`` means unknown and not enforced."""

    model_config = _MODEL

    max_concurrency: PositiveInt = 2
    rpm: PositiveInt | None = None  # not exposed by Groq; not measured in v1
    tpm: PositiveInt | None = 8000  # v1 P02-LIVE: x-ratelimit-limit-tokens
    rpd: PositiveInt | None = 1000  # v1 P02-LIVE: x-ratelimit-limit-requests
    tpd: PositiveInt | None = None  # not exposed by Groq; not measured in v1
    daily_safety_fraction: Annotated[float, Field(gt=0.0, le=1.0)] = 0.75


class QueueSettings(BaseModel):
    """Admission queue inputs. Capacity is derived (v1 ARCHITECTURE §15) unless set."""

    model_config = _MODEL

    patience_median_s: Annotated[float, Field(gt=0.0)] = 20.0
    tokens_per_answer: Annotated[float, Field(gt=0.0)] = 1513.0  # v1 P07, real small_fast
    capacity: PositiveInt | None = None  # explicit override of the derived capacity
    max_active: PositiveInt | None = None  # default: the guard's max_concurrency


class PathSettings(BaseModel):
    model_config = _MODEL

    papers_dir: Path = Path("data/papers")
    index_dir: Path = Path("data/indexes")
    models_dir: Path = Path("data/models")
    store_path: Path = Path("data/relay.sqlite3")  # used from V2-02
    # V2-03: uploaded PDFs ({sha256}.pdf) and per-collection index files. The v1 indexes in
    # index_dir are only read: the v1-papers collection points at them.
    uploads_dir: Path = Path("data/uploads")
    collections_dir: Path = Path("data/collections")


class IngestionSettings(BaseModel):
    """Upload limits and the ingest worker (V2-03). Active in ``cache.mode = "sqlite"``."""

    model_config = _MODEL

    max_upload_mb: Annotated[float, Field(gt=0.0, le=1024.0)] = 50.0
    max_pages: PositiveInt = 500
    poll_interval_s: Annotated[float, Field(gt=0.0)] = 2.0
    max_attempts: PositiveInt = 3
    max_loaded_collections: PositiveInt = 8

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)


class CacheSettings(BaseModel):
    model_config = _MODEL

    mode: CacheMode = "memory"  # "sqlite" arrives with the metadata store (V2-02)


class RetrievalConfig(BaseModel):
    model_config = _MODEL

    settings: Annotated[tuple[str, ...], Field(min_length=1)] = (
        "small_fast", "balanced_rerank", "large_wide")

    @model_validator(mode="after")
    def _known(self) -> RetrievalConfig:
        unknown = [s for s in self.settings if s not in RETRIEVAL_SETTINGS]
        if unknown:
            raise ValueError(f"unknown retrieval settings {unknown}; "
                             f"known: {sorted(RETRIEVAL_SETTINGS)}")
        if len(set(self.settings)) != len(self.settings):
            raise ValueError("retrieval settings must not repeat")
        return self


def _default_lanes() -> tuple[LaneSettings, ...]:
    return (LaneSettings(name="standin", kind="standin", model="qwen/qwen3.8-27b",
                         embedded=True, profile=CALIBRATED_PROFILE),)


class RelaySettings(BaseModel):
    model_config = _MODEL

    server: ServerSettings = ServerSettings()
    lanes: Annotated[tuple[LaneSettings, ...], Field(min_length=1)] = Field(
        default_factory=_default_lanes)
    guard: GuardSettings = GuardSettings()
    queue: QueueSettings = QueueSettings()
    paths: PathSettings = PathSettings()
    cache: CacheSettings = CacheSettings()
    retrieval: RetrievalConfig = RetrievalConfig()
    ingestion: IngestionSettings = IngestionSettings()

    @model_validator(mode="after")
    def _unique_lanes(self) -> RelaySettings:
        names = [lane.name for lane in self.lanes]
        if len(set(names)) != len(names):
            raise ValueError(f"lane names must be unique: {names}")
        return self

    @property
    def primary_lane(self) -> LaneSettings:
        return self.lanes[0]


# --- Loading ----------------------------------------------------------------------------


def _env_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def env_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    """Nested overrides from ``RELAY_SECTION__KEY`` variables (names are lower-cased)."""
    overrides: dict[str, Any] = {}
    for name, raw in environ.items():
        if not name.upper().startswith(ENV_PREFIX):
            continue
        parts = name[len(ENV_PREFIX):].lower().split(ENV_DELIMITER)
        if len(parts) < 2 or not all(parts):
            continue  # RELAY_FOO is not a settings override; leave it alone
        node = overrides
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ConfigurationError(f"conflicting environment overrides for {name}")
        node[parts[-1]] = _env_value(raw)
    return overrides


def _merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _describe(exc: ValidationError) -> str:
    """Field locations and messages only; input values are not echoed (they may be keys)."""
    parts = []
    for err in exc.errors():
        location = ".".join(str(p) for p in err["loc"]) or "settings"
        parts.append(f"{location}: {err['msg']}")
    return "; ".join(parts)


def with_overrides(settings: RelaySettings, patch: Mapping[str, Any]) -> RelaySettings:
    """A re-validated copy with ``patch`` merged in (used for command-line overrides)."""
    try:
        return RelaySettings.model_validate(_merge(settings.model_dump(), patch))
    except ValidationError as exc:
        raise ConfigurationError(f"invalid relay settings: {_describe(exc)}") from None


def load_relay_settings(path: Path | None = None, *,
                        environ: Mapping[str, str] | None = None) -> RelaySettings:
    """Defaults < TOML file < ``RELAY_*`` environment variables.

    ``path=None`` reads ``relay.toml`` if it exists and otherwise uses the defaults. An
    explicitly given path must exist.
    """
    data: dict[str, Any] = {}
    file = path if path is not None else DEFAULT_CONFIG_FILE
    if file.is_file():
        try:
            data = tomllib.loads(file.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(f"{file} is not valid TOML: {exc}") from None
    elif path is not None:
        raise ConfigurationError(f"config file not found: {file}")
    data = _merge(data, env_overrides(os.environ if environ is None else environ))
    try:
        return RelaySettings.model_validate(data)
    except ValidationError as exc:
        raise ConfigurationError(f"invalid relay settings: {_describe(exc)}") from None


__all__ = [
    "CALIBRATED_PROFILE",
    "DEFAULT_CONFIG_FILE",
    "CacheSettings",
    "GuardSettings",
    "IngestionSettings",
    "LaneSettings",
    "PathSettings",
    "QueueSettings",
    "RelaySettings",
    "RetrievalConfig",
    "ServerSettings",
    "env_overrides",
    "load_relay_settings",
    "with_overrides",
]
