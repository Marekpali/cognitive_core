"""observation_mode option (ADR 8): defaults and fail-safe to shadow."""

import json

import pytest

from src.options import read_observation_mode


def _options(tmp_path, content):
    path = tmp_path / "options.json"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv("OBSERVATION_MODE", raising=False)


def test_default_is_shadow(tmp_path):
    assert read_observation_mode(tmp_path / "missing.json") == "shadow"


def test_explicit_active(tmp_path):
    path = _options(tmp_path, json.dumps({"observation_mode": "active"}))
    assert read_observation_mode(path) == "active"


def test_options_file_wins_over_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSERVATION_MODE", "active")
    path = _options(tmp_path, json.dumps({"observation_mode": "shadow"}))
    assert read_observation_mode(path) == "shadow"


def test_environment_used_when_option_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSERVATION_MODE", "active")
    assert read_observation_mode(_options(tmp_path, "{}")) == "active"


@pytest.mark.parametrize("content", [
    json.dumps({"observation_mode": "ACTIVE"}),
    json.dumps({"observation_mode": True}),
    "not json",
    "[]",
])
def test_invalid_values_fall_back_to_shadow(tmp_path, content):
    assert read_observation_mode(_options(tmp_path, content)) == "shadow"
