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


class LanePricingSettings(BaseModel):
    """US dollars per million tokens, from the provider's price list. Unset: cost unknown."""

    model_config = _MODEL

    input_per_m: Annotated[float, Field(ge=0.0)]
    output_per_m: Annotated[float, Field(ge=0.0)]


class LaneGuardSettings(BaseModel):
    """Per-lane overrides of ``[guard]`` (V2-09). An unset field takes the ``[guard]``
    value; every lane still gets its own ProviderGuard (its own windows and budget)."""

    model_config = _MODEL

    max_concurrency: PositiveInt | None = None
    rpm: PositiveInt | None = None
    tpm: PositiveInt | None = None
    rpd: PositiveInt | None = None
    tpd: PositiveInt | None = None
    daily_safety_fraction: Annotated[float, Field(gt=0.0, le=1.0)] | None = None


class LaneSettings(BaseModel):
    """One (provider, model) pair. The first lane is the primary (V2-09 router)."""

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
    # V2-09 router
    pricing: LanePricingSettings | None = None
    guard: LaneGuardSettings | None = None
    escalate_to: Slug | None = None  # a truncated or empty answer is retried there once
    is_reasoning_model: bool = False

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
    # V2-10 fair admission. Each tenant's bucket refills at this share of the service rate;
    # a request over it is not refused, it waits behind requests within their rate.
    tenant_rate_share: Annotated[float, Field(gt=0.0, le=1.0)] = 0.5
    tenant_burst: Annotated[float, Field(ge=1.0)] = 4.0  # requests a tenant may send at once
    # re-derive the capacity from an EWMA of measured tokens per answer (off when
    # `capacity` is set explicitly)
    adaptive_capacity: bool = True
    ewma_alpha: Annotated[float, Field(gt=0.0, le=1.0)] = 0.2


class AuthSettings(BaseModel):
    """API keys (V2-10). Keys are made with ``relay keys create``; only their SHA-256 is
    stored. The development key ``relay-dev-key`` (the web UI's default) is the development
    tenant: ``None`` accepts it only while the server listens on a loopback address."""

    model_config = _MODEL

    dev_key_enabled: bool | None = None


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


class VerifierSettings(BaseModel):
    """L4, the advisory entailment score (V2-07). The model is downloaded with
    ``scripts/download_l4_model.py``; when its files are absent, L4 is off."""

    model_config = _MODEL

    l4_model_dir: Path = Path("data/models/nli/nli-MiniLM2-L6-H768")
    # V2-07 spike (14-core laptop, real Table 3 pairs): int8 for AVX2 CPUs, ~90 MB. Use
    # "onnx/model.onnx" without AVX2. V2-08: ONNX threads per call are capped at 4 so
    # concurrent requests do not starve the CPU (the scorer enforces the cap).
    l4_model_file: str = "onnx/model_quint8_avx2.onnx"
    l4_threads: PositiveInt = 4  # ONNX threads per scoring call (at most 4)


class RewriterSettings(BaseModel):
    """Follow-up rewriting on POST /v1/ask (V2-08). ``auto``: the LLM rewriter on a Groq
    lane, the rule rewriter on a stand-in lane (the stand-in cannot rewrite)."""

    model_config = _MODEL

    mode: Literal["auto", "llm", "rules"] = "auto"
    model: Annotated[str, Field(min_length=1, max_length=128)] | None = None  # None: lane's
    timeout_s: Annotated[float, Field(gt=0, le=10)] = 1.5  # then the original question
    max_completion_tokens: Annotated[int, Field(ge=8, le=256)] = 64


class ProvidersSettings(BaseModel):
    """``[providers]``: the router's settings. The lanes are ``[[providers.lanes]]`` (the
    older top-level ``[[lanes]]`` still works)."""

    model_config = _MODEL

    # Rules 1-2: a lane is used when its guard can admit the request within this wait;
    # otherwise the next lane is tried. Past every lane: the shortest wait, up to the
    # deadline (rule 3). With one lane this has no effect.
    max_wait_ms: Annotated[float, Field(ge=0.0)] = 2000.0


def _default_lanes() -> tuple[LaneSettings, ...]:
    # The stand-in is simulated: no money is spent, so its price is 0 (a Groq lane has no
    # default price: its cost stays unknown until the owner sets one).
    return (LaneSettings(name="standin", kind="standin", model="qwen/qwen3.8-27b",
                         embedded=True, profile=CALIBRATED_PROFILE,
                         pricing=LanePricingSettings(input_per_m=0.0, output_per_m=0.0)),)


class RelaySettings(BaseModel):
    model_config = _MODEL

    server: ServerSettings = ServerSettings()
    lanes: Annotated[tuple[LaneSettings, ...], Field(min_length=1)] = Field(
        default_factory=_default_lanes)
    providers: ProvidersSettings = ProvidersSettings()
    guard: GuardSettings = GuardSettings()
    queue: QueueSettings = QueueSettings()
    paths: PathSettings = PathSettings()
    cache: CacheSettings = CacheSettings()
    retrieval: RetrievalConfig = RetrievalConfig()
    ingestion: IngestionSettings = IngestionSettings()
    verifier: VerifierSettings = VerifierSettings()
    rewriter: RewriterSettings = RewriterSettings()
    auth: AuthSettings = AuthSettings()

    @model_validator(mode="before")
    @classmethod
    def _provider_lanes(cls, data: Any) -> Any:
        """``[[providers.lanes]]`` (V2-09) is the same list as the older ``[[lanes]]``."""
        if not isinstance(data, dict):
            return data
        providers = data.get("providers")
        if not isinstance(providers, dict) or "lanes" not in providers:
            return data
        if "lanes" in data:
            raise ValueError("set the lanes once: [[providers.lanes]] or [[lanes]], not both")
        rest = {key: value for key, value in providers.items() if key != "lanes"}
        return {**data, "providers": rest, "lanes": providers["lanes"]}

    @model_validator(mode="after")
    def _unique_lanes(self) -> RelaySettings:
        names = [lane.name for lane in self.lanes]
        if len(set(names)) != len(names):
            raise ValueError(f"lane names must be unique: {names}")
        for lane in self.lanes:
            if lane.escalate_to is not None and (lane.escalate_to not in names
                                                 or lane.escalate_to == lane.name):
                raise ValueError(f"lane {lane.name!r}: escalate_to must name another lane")
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
    "AuthSettings",
    "CacheSettings",
    "GuardSettings",
    "IngestionSettings",
    "LaneGuardSettings",
    "LanePricingSettings",
    "LaneSettings",
    "ProvidersSettings",
    "PathSettings",
    "QueueSettings",
    "RelaySettings",
    "RetrievalConfig",
    "ServerSettings",
    "env_overrides",
    "load_relay_settings",
    "with_overrides",
]
