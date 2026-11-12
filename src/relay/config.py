"""Typed runtime configuration loaded from environment variables / `.env`.

Only values that are decided or safely defaulted live here. Numbers that must come
from measurement (queue capacity, Groq limits, prices) are deliberately absent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from relay.errors import ConfigurationError
from relay.redaction import redact_secrets

ProviderMode = Literal["scripted", "standin", "groq"]
CacheMode = Literal["off", "exact"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    provider_mode: ProviderMode = "scripted"
    groq_api_key: SecretStr | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # Initial candidate only (ARCHITECTURE.md section 10); not a final model choice.
    groq_model: str = "openai/gpt-oss-20b"
    standin_url: str = "http://127.0.0.1:8001/openai/v1"
    cache_mode: CacheMode = "exact"

    evidence_dir: Path = Path("evidence")
    scratch_dir: Path = Path("scratch")
    results_path: Path = Path("results.csv")

    # Hard cap on real provider calls per script run. 0 = no real calls permitted.
    max_real_calls: int = Field(default=0, ge=0)
    # [OWNER-DECIDED 2026-11-12] fraction of known daily limits at which real runs stop.
    daily_safety_fraction: float = Field(default=0.75, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _require_key_for_groq(self) -> Settings:
        if self.provider_mode != "groq":
            return self
        if self.groq_api_key is None or not self.groq_api_key.get_secret_value().strip():
            raise ValueError("GROQ_API_KEY must be set when PROVIDER_MODE=groq")
        return self

    def manifest_snapshot(self) -> dict[str, Any]:
        """Configuration safe to persist in run manifests (the key itself is never included)."""
        snapshot = self.model_dump(mode="json", exclude={"groq_api_key"})
        # Named so the conservative redactor (which masks any "*api_key*" field) keeps it.
        snapshot["groq_credential_configured"] = self.groq_api_key is not None
        return redact_secrets(snapshot)


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    """Load settings; ``env_file=None`` reads only process environment variables."""
    try:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(part) for part in err["loc"]) or "settings" for err in exc.errors()}
        )
        raise ConfigurationError(f"invalid configuration for: {', '.join(fields)}") from None
