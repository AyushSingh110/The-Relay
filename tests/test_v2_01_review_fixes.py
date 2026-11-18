"""V2-01 regression tests for the code-review findings (each failed before its fix)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from relay import cli
from relay.app import compose
from relay.app.settings import LaneSettings, RelaySettings
from relay.config import Settings
from relay.errors import ConfigurationError
from tests.test_pipeline_and_verifier import PASSAGES, StubRetriever
from tests.test_standin import MODEL, profile


def env() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def doctor_text(settings: RelaySettings) -> tuple[int, str]:
    lines: list[str] = []
    return cli.doctor(settings, env(), "test", out=lines.append), "\n".join(lines)


def test_invalid_cli_port_is_a_clean_configuration_error(
        capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["serve", "--port", "0", "--env-file", "none.env"]) == cli.EXIT_FAILED
    err = capsys.readouterr().err
    assert "relay serve:" in err and "server.port" in err and "Traceback" not in err


@pytest.mark.parametrize("content", [None, "{not json", '{"label": "calibrated"}'],
                         ids=["missing", "not-json", "invalid-profile"])
def test_unusable_standin_profile_is_a_configuration_error(tmp_path: Path,
                                                           content: str | None) -> None:
    path = tmp_path / "profile.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    lane = LaneSettings(name="s", kind="standin", model=MODEL, embedded=True, profile=path)
    with pytest.raises(ConfigurationError, match="stand-in profile"):
        compose.standin_profile(lane)


@pytest.mark.parametrize("content", ["{truncated", "[1, 2]"], ids=["not-json", "not-object"])
def test_doctor_reports_a_corrupt_corpus_manifest_instead_of_crashing(
        tmp_path: Path, content: str) -> None:
    (tmp_path / "manifest.json").write_text(content, encoding="utf-8")
    code, text = doctor_text(RelaySettings(paths={"papers_dir": tmp_path}))  # type: ignore[arg-type]
    assert code == cli.EXIT_FAILED and "[FAIL] corpus manifest" in text


def test_doctor_uses_the_same_rules_as_serve() -> None:
    no_limits = RelaySettings(guard={"tpm": None, "rpm": None})  # type: ignore[arg-type]
    code, text = doctor_text(no_limits)
    assert code == cli.EXIT_FAILED and "[FAIL] admission capacity" in text
    absent = RelaySettings(lanes=({"name": "s", "kind": "standin",  # type: ignore[arg-type]
                                   "model": "absent-model", "embedded": True},))
    code, text = doctor_text(absent)
    assert code == cli.EXIT_FAILED and "has no model 'absent-model'" in text


def test_example_file_does_not_claim_that_omitting_a_limit_disables_it() -> None:
    example = (Path(__file__).resolve().parents[1] / "relay.example.toml").read_text(
        encoding="utf-8")
    assert "Leave a value out" not in example
    assert "RELAY_GUARD__RPD=null" in example


async def test_retrievers_load_off_the_event_loop_before_the_standin_starts(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    main_thread = threading.current_thread()

    def fake_load(_: RelaySettings) -> tuple[dict[str, Any], frozenset[str]]:
        seen["thread"] = threading.current_thread()
        seen["loaded_before_standin"] = not seen.get("standin_started", False)
        return {"small_fast": StubRetriever(PASSAGES)}, frozenset()

    real_serve = compose.serve_on_loopback

    def recording_serve(app: Any) -> Any:
        seen["standin_started"] = True
        return real_serve(app)

    monkeypatch.setattr(compose, "_default_retrievers", fake_load)
    monkeypatch.setattr(compose, "serve_on_loopback", recording_serve)
    path = tmp_path / "profile.json"
    path.write_text(profile().model_dump_json(), encoding="utf-8")
    settings = RelaySettings(
        lanes=(LaneSettings(name="s", kind="standin", model=MODEL, embedded=True,
                            profile=path),),
        retrieval={"settings": ("small_fast",)})  # type: ignore[arg-type]
    async with compose.open_runtime(settings, env()):
        pass
    assert seen["thread"] is not main_thread  # loaded in a worker thread
    assert seen["loaded_before_standin"] is True and seen["standin_started"] is True
