"""V2-01 tests: relay settings (defaults, TOML file, environment overrides, validation)."""

from __future__ import annotations

from pathlib import Path

import pytest

from relay.app.settings import (
    CALIBRATED_PROFILE,
    RelaySettings,
    env_overrides,
    load_relay_settings,
)
from relay.errors import ConfigurationError
from tests.factories import FAKE_KEY

EXAMPLE = Path(__file__).resolve().parents[1] / "relay.example.toml"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "relay.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_serve_the_v1_papers_from_an_embedded_calibrated_standin() -> None:
    settings = RelaySettings()
    lane = settings.primary_lane
    assert (lane.kind, lane.model, lane.embedded) == ("standin", "qwen/qwen3.8-27b", True)
    assert lane.profile == CALIBRATED_PROFILE
    assert (settings.guard.tpm, settings.guard.rpd, settings.guard.rpm) == (8000, 1000, None)
    assert settings.guard.daily_safety_fraction == 0.75
    assert settings.cache.mode == "memory"
    assert settings.retrieval.settings == ("small_fast", "balanced_rerank", "large_wide")
    assert settings.paths.index_dir == Path("data/indexes")


def test_the_example_file_parses_to_the_defaults() -> None:
    assert load_relay_settings(EXAMPLE, environ={}) == RelaySettings()


def test_missing_default_file_uses_defaults_but_an_explicit_path_must_exist(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_relay_settings(environ={}) == RelaySettings()
    with pytest.raises(ConfigurationError, match="not found"):
        load_relay_settings(tmp_path / "nope.toml", environ={})


def test_toml_values_override_defaults(tmp_path: Path) -> None:
    path = write(tmp_path, """
[server]
port = 9100

[[lanes]]
name = "groq"
kind = "groq"
model = "qwen/qwen3.8-27b"

[guard]
tpm = 4000
rpm = 30

[cache]
mode = "off"

[retrieval]
settings = ["small_fast"]
""")
    settings = load_relay_settings(path, environ={})
    assert settings.server.port == 9100 and settings.primary_lane.kind == "groq"
    assert (settings.guard.tpm, settings.guard.rpm, settings.guard.rpd) == (4000, 30, 1000)
    assert settings.cache.mode == "off" and settings.retrieval.settings == ("small_fast",)


def test_environment_overrides_win_over_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, "[server]\nport = 9100\n")
    environ = {"RELAY_SERVER__PORT": "9200", "RELAY_CACHE__MODE": "off",
               "RELAY_GUARD__RPM": "null", "RELAY_RETRIEVAL__SETTINGS": '["large_wide"]',
               "RELAY_QUEUE__PATIENCE_MEDIAN_S": "12.5", "RELAY_UNRELATED": "x",
               "GROQ_API_KEY": FAKE_KEY}
    settings = load_relay_settings(path, environ=environ)
    assert settings.server.port == 9200 and settings.cache.mode == "off"
    assert settings.retrieval.settings == ("large_wide",)
    assert settings.queue.patience_median_s == 12.5 and settings.guard.rpm is None
    assert env_overrides({"RELAY_SERVER__HOST": "0.0.0.0"}) == {"server": {"host": "0.0.0.0"}}


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("[server]\nport = 0\n", "server.port"),
        ("[guard]\ngroq_api_key = 'x'\n", "groq_api_key"),
        ("[retrieval]\nsettings = ['nope']\n", "unknown retrieval settings"),
        ("[cache]\nmode = 'redis'\n", "cache.mode"),
        ("[[lanes]]\nname = 'a'\nkind = 'groq'\nmodel = 'm'\nembedded = true\n",
         "stand-in lanes only"),
        ("[[lanes]]\nname = 'a'\nkind = 'standin'\nmodel = 'm'\n", "embedded = true"),
        ("[[lanes]]\nname = 'a'\nkind = 'groq'\nmodel = 'm'\n"
         "[[lanes]]\nname = 'a'\nkind = 'groq'\nmodel = 'n'\n", "unique"),
        ("lanes = []\n", "lanes"),
        ("not toml ===\n", "not valid TOML"),
    ],
    ids=["port", "secret-field", "retrieval", "cache-mode", "groq-embedded", "standin-no-url",
         "duplicate-lanes", "no-lanes", "bad-toml"],
)
def test_invalid_settings_fail_loudly_without_echoing_values(
        tmp_path: Path, text: str, problem: str) -> None:
    with pytest.raises(ConfigurationError, match=problem) as caught:
        load_relay_settings(write(tmp_path, text), environ={})
    assert "'x'" not in str(caught.value)  # rejected values are not repeated back


def test_secret_values_in_the_environment_never_reach_error_messages(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_relay_settings(write(tmp_path, ""), environ={"RELAY_SERVER__PORT": FAKE_KEY})
    assert FAKE_KEY not in str(caught.value)
