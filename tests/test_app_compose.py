"""V2-01 tests: the composition root builds a working service from settings. Offline.

The embedded stand-in runs under uvicorn on 127.0.0.1; retrieval is a scripted stub so no
ONNX model is loaded. No API key is used or needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from relay.app.compose import (
    capacity_derivation,
    corpus_sha256,
    guard_config,
    open_runtime,
    standin_profile,
)
from relay.app.settings import LaneSettings, RelaySettings
from relay.config import Settings
from relay.errors import ConfigurationError
from tests.factories import FAKE_KEY
from tests.test_pipeline_and_verifier import PASSAGES, StubRetriever
from tests.test_standin import MODEL, profile


def env(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def settings_with(tmp_path: Path, **lane: Any) -> RelaySettings:
    path = tmp_path / "profile.json"
    path.write_text(profile().model_dump_json(), encoding="utf-8")
    values: dict[str, Any] = {"name": "standin", "kind": "standin", "model": MODEL,
                              "embedded": True, "profile": path, **lane}
    return RelaySettings(lanes=(LaneSettings(**values),),
                         retrieval={"settings": ("small_fast",)})  # type: ignore[arg-type]


RETRIEVERS = {"small_fast": StubRetriever(PASSAGES)}


def test_guard_and_capacity_follow_the_settings() -> None:
    settings = RelaySettings()
    config = guard_config(settings)
    assert (config.tpm_limit, config.rpd_limit, config.rpm_limit) == (8000, 1000, None)
    assert config.max_concurrency == 2 and config.daily_safety_fraction == 0.75
    derived = capacity_derivation(settings)
    assert derived.capacity == 1 and derived.binding_bound == "tpm"  # floor(0.088 x 20)
    override = RelaySettings(queue={"capacity": 7})  # type: ignore[arg-type]
    assert capacity_derivation(override).capacity == 7
    with pytest.raises(ConfigurationError, match="guard.tpm or guard.rpm"):
        capacity_derivation(RelaySettings(guard={"tpm": None}))  # type: ignore[arg-type]


def test_the_default_profile_and_corpus_are_available() -> None:
    settings = RelaySettings()
    assert MODEL not in standin_profile(settings.primary_lane).models  # calibrated profile
    assert "qwen/qwen3.8-27b" in standin_profile(settings.primary_lane).models
    assert len(corpus_sha256(settings)) == 64


async def test_open_runtime_serves_post_ask_from_the_embedded_standin(tmp_path: Path) -> None:
    settings = settings_with(tmp_path)
    async with open_runtime(settings, env(), retrievers=RETRIEVERS) as runtime:
        assert runtime.source == "stand-in" and runtime.real_call_cap is None
        assert runtime.provider_url.startswith("http://127.0.0.1:")
        transport = httpx.ASGITransport(app=runtime.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            reply = await client.post("/ask", json={
                "request_id": "req-1", "question": "What is d_model in the encoder?",
                "retrieval_setting": "small_fast"})
            health = (await client.get("/healthz")).json()
    assert reply.status_code == 200 and reply.text.startswith("event: answer")
    final = json.loads(reply.text.split("event: final\ndata: ")[1])
    assert (final["status"], final["provider_called"], final["model_id"]) == ("ok", True, MODEL)
    assert health["cache_mode"] == "exact" and health["queue_capacity"] == 1
    assert any("lane: standin" in line for line in runtime.describe())


async def test_cache_mode_off_is_wired_through(tmp_path: Path) -> None:
    settings = RelaySettings.model_validate(
        {**settings_with(tmp_path).model_dump(), "cache": {"mode": "off"}})
    async with open_runtime(settings, env(), retrievers=RETRIEVERS) as runtime:
        assert runtime.service.cache_mode == "off"


@pytest.mark.parametrize(
    ("change", "env_values", "problem"),
    [
        ({"cache": {"mode": "sqlite"}}, {}, "V2-02"),
        ({"lanes": [{"name": "g", "kind": "groq", "model": "m"}]}, {}, "GROQ_API_KEY"),
        ({"lanes": [{"name": "g", "kind": "groq", "model": "m"}]},
         {"groq_api_key": SecretStr(FAKE_KEY), "max_real_calls": 0}, "MAX_REAL_CALLS"),
        ({"lanes": [{"name": "s", "kind": "standin", "model": "absent-model",
                     "embedded": True}]}, {}, "no model"),
    ],
    ids=["sqlite-cache", "groq-no-key", "groq-no-cap", "profile-model"],
)
async def test_open_runtime_refuses_unusable_settings_with_a_clear_reason(
        change: dict[str, Any], env_values: dict[str, Any], problem: str) -> None:
    settings = RelaySettings.model_validate({**RelaySettings().model_dump(), **change})
    with pytest.raises(ConfigurationError, match=problem) as caught:
        async with open_runtime(settings, env(**env_values), retrievers=RETRIEVERS):
            pass
    assert FAKE_KEY not in str(caught.value)


async def test_groq_lane_is_built_with_a_hard_call_cap_and_never_called() -> None:
    settings = RelaySettings.model_validate({**RelaySettings().model_dump(), "lanes": [
        {"name": "g", "kind": "groq", "model": "qwen/qwen3.8-27b",
         "base_url": "http://127.0.0.1:9/openai/v1"}]})
    async with open_runtime(settings, env(groq_api_key=SecretStr(FAKE_KEY), max_real_calls=3),
                            retrievers=RETRIEVERS) as runtime:
        assert (runtime.source, runtime.real_call_cap) == ("real", 3)
        assert all(FAKE_KEY not in line for line in runtime.describe())
