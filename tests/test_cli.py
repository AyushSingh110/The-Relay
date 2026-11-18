"""V2-01 tests: ``relay doctor``, ``relay ingest`` and argument handling. Offline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from relay import cli
from relay.app.settings import RelaySettings
from relay.config import Settings
from tests.factories import FAKE_KEY


def env(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def run_doctor(settings: RelaySettings, environment: Settings) -> tuple[int, str]:
    lines: list[str] = []
    code = cli.doctor(settings, environment, "test", out=lines.append)
    return code, "\n".join(lines)


def test_doctor_passes_on_the_repository_defaults_and_needs_no_key() -> None:
    code, text = run_doctor(RelaySettings(), env())
    assert code == cli.EXIT_OK, text
    assert "[  OK] corpus manifest" in text and "(3 papers)" in text
    assert "[  OK] index directory" in text and "(3/3 presets)" in text
    assert "GROQ_API_KEY: not set (not needed: no groq lane is configured)" in text
    assert "embedded stand-in, model qwen/qwen3.8-27b, profile calibrated" in text
    assert text.endswith("doctor: all required checks passed")


def test_doctor_reports_a_missing_index_and_corpus_clearly(tmp_path: Path) -> None:
    settings = RelaySettings(paths={"papers_dir": tmp_path / "papers",  # type: ignore[arg-type]
                                    "index_dir": tmp_path / "indexes",
                                    "models_dir": tmp_path / "models"})
    code, text = run_doctor(settings, env())
    assert code == cli.EXIT_FAILED
    assert "[FAIL] index directory" in text and "run scripts/build_indexes.py" in text
    assert "[FAIL] corpus manifest" in text and "[FAIL] embedding models" in text
    assert text.endswith("3 check(s) failed")


def test_doctor_reports_a_partial_index(tmp_path: Path) -> None:
    (tmp_path / "small_fast").mkdir()
    (tmp_path / "small_fast" / "index_manifest.json").write_text("{}", encoding="utf-8")
    settings = RelaySettings(paths={"index_dir": tmp_path})  # type: ignore[arg-type]
    code, text = run_doctor(settings, env())
    assert code == cli.EXIT_FAILED and "(1/3 presets; missing ['balanced_rerank', 'large_wide']" \
        in text


def test_doctor_checks_the_key_and_call_cap_for_a_groq_lane_without_printing_the_key() -> None:
    groq = RelaySettings(lanes=({"name": "g", "kind": "groq",  # type: ignore[arg-type]
                                 "model": "qwen/qwen3.8-27b"},))
    code, text = run_doctor(groq, env())
    assert code == cli.EXIT_FAILED and "[FAIL] GROQ_API_KEY: not set" in text
    code, text = run_doctor(groq, env(groq_api_key=SecretStr(FAKE_KEY), max_real_calls=5))
    assert code == cli.EXIT_OK and "[  OK] GROQ_API_KEY: set" in text
    assert "MAX_REAL_CALLS: 5" in text and FAKE_KEY not in text and "gsk_" not in text
    code, text = run_doctor(groq, env(groq_api_key=SecretStr(FAKE_KEY), max_real_calls=0))
    assert code == cli.EXIT_FAILED and "[FAIL] MAX_REAL_CALLS: 0" in text


def test_doctor_flags_a_bad_profile(tmp_path: Path) -> None:
    # Since V2-02 the sqlite cache mode is valid; its doctor checks are in
    # tests/test_store_integration.py and use an in-memory store.
    bad = tmp_path / "profile.json"
    bad.write_text("{not json", encoding="utf-8")
    settings = RelaySettings(lanes=({"name": "s", "kind": "standin",  # type: ignore[arg-type]
                                     "model": "m", "embedded": True, "profile": bad},))
    code, text = run_doctor(settings, env())
    assert code == cli.EXIT_FAILED
    # doctor reports the composition root's own messages (one rule for doctor and serve)
    assert f"[FAIL] lane s: stand-in profile {bad} could not be loaded" in text
    assert "[INFO] store: not used (cache mode memory)" in text


def test_ingest_is_a_clear_stub(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["ingest"]) == cli.EXIT_UNAVAILABLE
    assert "arrive in V2-03" in capsys.readouterr().out


def test_main_doctor_reads_settings_and_reports_config_errors(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    good = tmp_path / "relay.toml"
    good.write_text("[cache]\nmode = 'off'\n", encoding="utf-8")
    code = cli.main(["doctor", "--config", str(good), "--env-file", str(tmp_path / "none")])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK and f"config: {good}" in out
    bad = tmp_path / "bad.toml"
    bad.write_text("[server]\nport = 0\n", encoding="utf-8")
    assert cli.main(["doctor", "--config", str(bad)]) == cli.EXIT_FAILED
    assert "invalid relay settings: server.port" in capsys.readouterr().err


def test_unknown_commands_are_usage_errors() -> None:
    with pytest.raises(SystemExit) as caught:
        cli.parse_args(["launch"])
    assert caught.value.code == 2
