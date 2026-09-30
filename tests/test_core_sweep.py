"""CognitiveCore wiring for STEP 7P: mode from options, sweep on snapshot."""

import asyncio

import pytest

from src import core as core_module
from src.core import CognitiveCore
from tests.ha_payloads import full_home


@pytest.fixture
def make_core(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "core.db"))
    pushes = []
    monkeypatch.setattr(core_module, "update_pending_reviews", pushes.append)

    def build(mode):
        monkeypatch.setattr(core_module, "read_observation_mode", lambda: mode)
        core = CognitiveCore(knowledge_path=tmp_path)
        asyncio.run(core.load_classifiers())
        return core, pushes
    return build


def _count(core, table):
    return core.storage.connect().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_shadow_core_writes_no_observations_and_pushes_nothing(make_core):
    core, pushes = make_core("shadow")

    async def go():
        await core.on_snapshot("startup", *full_home())
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert _count(core, "classification_observations") == 0
    assert _count(core, "classification_sweeps") == 1
    assert pushes == []
    assert asyncio.run(core.on_snapshot("daily", *full_home())) == 0  # missing-metadata count


def test_active_core_writes_observations_and_refreshes_sensor(make_core):
    core, pushes = make_core("active")

    async def go():
        await core.on_snapshot("startup", *full_home())
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert _count(core, "classification_observations") == 4
    assert pushes == [core.storage]
