"""STEP 7a option `precedent_mode`: off unless explicitly and validly set to
shadow; the add-on config matches the code; Core writes the layer cut-off
at its first start in shadow, before any sweep."""

import asyncio
import json
import re
from pathlib import Path

import pytest

from src import core as core_module
from src.core import CognitiveCore
from src.options import (
    DEFAULT_PRECEDENT_MODE, PRECEDENT_MODES, read_observation_mode, read_precedent_mode,
)
from tests.ha_payloads import full_home
from tests.precedent_helpers import count, rows

CONFIG = (Path(__file__).resolve().parent.parent / "haos_addon" / "config.yaml").read_text(
    encoding="utf-8")


def _options(tmp_path, content):
    path = tmp_path / "options.json"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv("PRECEDENT_MODE", raising=False)
    monkeypatch.delenv("OBSERVATION_MODE", raising=False)


def test_default_is_off(tmp_path):
    assert read_precedent_mode(tmp_path / "missing.json") == "off" == DEFAULT_PRECEDENT_MODE
    assert read_precedent_mode(_options(tmp_path, "{}")) == "off"
    assert PRECEDENT_MODES == ("off", "shadow")           # no 'active': 7a never acts


def test_explicit_shadow(tmp_path):
    assert read_precedent_mode(_options(tmp_path, json.dumps({"precedent_mode": "shadow"}))) == "shadow"


def test_options_file_wins_over_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PRECEDENT_MODE", "shadow")
    assert read_precedent_mode(_options(tmp_path, json.dumps({"precedent_mode": "off"}))) == "off"
    assert read_precedent_mode(_options(tmp_path, "{}")) == "shadow"


@pytest.mark.parametrize("content", [
    json.dumps({"precedent_mode": "SHADOW"}), json.dumps({"precedent_mode": "active"}),
    json.dumps({"precedent_mode": True}), "not json", "[]",
])
def test_invalid_values_fall_back_to_off(tmp_path, content, capsys):
    assert read_precedent_mode(_options(tmp_path, content)) == "off"
    assert "precedent_mode=off" in capsys.readouterr().out


def test_announce_can_be_silenced(tmp_path, capsys):
    read_precedent_mode(tmp_path / "missing.json", announce=False)
    assert capsys.readouterr().out == ""


def test_the_two_options_are_independent(tmp_path):
    path = _options(tmp_path, json.dumps({"observation_mode": "active", "precedent_mode": "shadow"}))
    assert (read_observation_mode(path), read_precedent_mode(path)) == ("active", "shadow")
    path = _options(tmp_path, json.dumps({"observation_mode": "active"}))
    assert (read_observation_mode(path), read_precedent_mode(path)) == ("active", "off")


def _block(name: str) -> str:
    return re.search(rf"^{name}:\n((?:  .*\n)+)", CONFIG, re.MULTILINE).group(1)


def test_addon_config_matches_the_code():
    assert re.search(r'^  precedent_mode: "?(\w+)"?$', _block("options"),
                     re.MULTILINE).group(1) == DEFAULT_PRECEDENT_MODE
    values = re.search(r"^  precedent_mode: list\(([^)]*)\)$", _block("schema"),
                       re.MULTILINE).group(1).split("|")
    assert tuple(values) == PRECEDENT_MODES


# --- Core -------------------------------------------------------------------------

@pytest.fixture
def make_core(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "core.db"))
    monkeypatch.setattr(core_module, "update_pending_reviews", lambda storage: None)
    monkeypatch.setattr(core_module, "read_observation_mode", lambda: "active")
    cores = []

    def build(mode):
        monkeypatch.setattr(core_module, "read_precedent_mode", lambda: mode)
        core = CognitiveCore(knowledge_path=tmp_path)

        async def no_adapters():
            return None
        monkeypatch.setattr(core, "load_adapters", no_adapters)
        cores.append(core)
        return core
    yield build
    for core in cores:
        core.storage.connection and core.storage.connection.close()


def test_core_in_off_never_writes_the_cut_off(make_core):
    core = make_core("off")

    async def go():
        await core.start()
        await core.on_snapshot("startup", *full_home())
    asyncio.run(go())
    assert core.storage.precedent_mode == "off"
    assert count(core.storage, "classification_observations") == 4
    assert all(count(core.storage, t) == 0 for t in (
        "precedent_audit", "precedent_annotations", "precedent_annotation_evidence"))


def test_core_in_shadow_writes_the_cut_off_before_the_first_sweep(make_core, capsys):
    core = make_core("shadow")
    assert count(core.storage, "precedent_audit") == 0       # not in the constructor

    async def go():
        await core.start()
        await core.on_snapshot("startup", *full_home())
    asyncio.run(go())
    (started,) = rows(core.storage, "precedent_audit")
    assert (started["event"], started["source"]) == ("layer_started", None)
    first_sweep = rows(core.storage, "classification_sweeps")[0]
    first_observation = rows(core.storage, "classification_observations")[0]
    assert started["created_at"] <= first_sweep["started_at"] <= first_observation["created_at"]
    assert f"[PRECEDENT] layer started at {started['created_at']}" in capsys.readouterr().out


def test_cut_off_is_kept_across_core_restarts(make_core):
    first = make_core("shadow")
    asyncio.run(first.start())
    (started,) = rows(first.storage, "precedent_audit")
    first.storage.connection.close()
    for mode in ("off", "shadow"):
        core = make_core(mode)
        asyncio.run(core.start())
        assert rows(core.storage, "precedent_audit") == [started]
        core.storage.connection.close()
