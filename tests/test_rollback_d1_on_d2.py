"""Rollback proof: D1-on-D2-schema = PASS.

D2 rollback A restores the D1 code but keeps the D2-migrated database
(new tables, input_id column, 5 triggers). These tests run the EXACT D1
production code (tests/legacy, hashes pinned to the D1 attestation) on a
database migrated by the D2 code and check: startup, the normal D1 write
path (core.on_device_detected -> observations, review cases, asset),
reads/analytics, D0 protection, D1 triggers, and that nothing raises
because of the new schema. Finally the D2 code must open the database
again (roll-forward) with every D1-written row intact.
"""

import asyncio
import hashlib
import importlib.util
import sqlite3
import warnings
from pathlib import Path

import pytest

from src.coverage_store import STEP7P_TRIGGERS
from src.storage import Storage as StorageD2
from src.sweep import ObservationSweep
from tests.ha_payloads import full_home
from tests.test_sweep import _classifiers

LEGACY = Path(__file__).resolve().parent / "legacy"
D1_HASHES = {
    "storage_d1.py": "6bb9c184420a45405d4b586a4b6964b2e912e049e01381080522e76d13bd3669",
    "core_d1.py": "c3a70399c9faab32956ffa837921c197b305ee1ae4e59010e55c72e4141fabb1",
}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, LEGACY / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


storage_d1 = _load("storage_d1")


@pytest.fixture
def d2_db(tmp_path):
    """A database as D2 leaves it: migrated, one shadow sweep, plus history."""
    path = tmp_path / "core.db"
    s = StorageD2(path)
    asyncio.run(ObservationSweep(s, _classifiers(), "shadow").run("startup", *full_home()))
    asyncio.run(ObservationSweep(s, _classifiers(), "active").run("startup", *full_home()))
    s.connection.close()
    return path


def _triggers(path) -> set:
    conn = sqlite3.connect(path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    conn.close()
    return names


def test_legacy_copies_are_the_exact_d1_production_code():
    for name, expected in D1_HASHES.items():
        assert hashlib.sha256((LEGACY / name).read_bytes()).hexdigest() == expected


def test_d1_storage_starts_on_d2_schema_without_error(d2_db):
    with warnings.catch_warnings():
        warnings.simplefilter("error")          # no warning from the new schema either
        s = storage_d1.Storage(d2_db)
    assert s.count_pending_reviews() >= 0
    s.connection.close()
    assert set(STEP7P_TRIGGERS) <= _triggers(d2_db)   # D1 startup leaves D2 intact


def test_d1_core_write_path_on_d2_schema(d2_db, monkeypatch):
    core_d1 = _load("core_d1")
    monkeypatch.setattr(core_d1, "Storage", storage_d1.Storage)
    monkeypatch.setattr(core_d1, "update_pending_reviews", lambda storage: None)
    monkeypatch.setenv("DB_PATH", str(d2_db))
    core = core_d1.CognitiveCore(knowledge_path=d2_db.parent)
    asyncio.run(core.load_classifiers())

    device = {  # D1 adapter shape: domain = platform, no device_class
        "id": "dev_rollback", "name": "Rollback socket", "manufacturer": "Tuya",
        "model": "Smart Socket", "entities": [
            {"name": "Power", "domain": "tuya", "entity_id": "sensor.rb_power"},
            {"name": "Total energy", "domain": "tuya", "entity_id": "sensor.rb_energy"}]}

    async def detect():
        await core.on_device_detected("ha", device)
        await asyncio.sleep(0.05)
    asyncio.run(detect())

    conn = sqlite3.connect(d2_db)
    conn.row_factory = sqlite3.Row
    obs = conn.execute("SELECT * FROM classification_observations "
                       "WHERE device_id = 'dev_rollback'").fetchall()
    assert len(obs) == 1 and obs[0]["classifier_name"] == "energy_meter"
    assert obs[0]["input_id"] is None           # D1 never sets it
    assert conn.execute("SELECT COUNT(*) FROM review_cases "
                        "WHERE device_id = 'dev_rollback'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM assets "
                        "WHERE source_device_id = 'dev_rollback'").fetchone()[0] == 1
    conn.close()
    core.storage.connection.close()


def test_d1_reads_review_and_d0_protection_on_d2_schema(d2_db):
    s = storage_d1.Storage(d2_db)
    cases = s.get_pending_review_cases()
    assert cases                                # written by the D2 active sweep
    case = cases[0]
    assert s.resolve_review_case(case["case_id"], case["observation_id"],
                                 decision="approved") == "resolved"
    # D0: a second decision is refused, by both paths
    assert s.resolve_review_case(case["case_id"], case["observation_id"],
                                 decision="rejected") == "already_resolved"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert s.reject_observation(case["observation_id"]) == "already_resolved"
    analytics = s.get_review_analytics()
    assert analytics["summary"]["approved"] == 1
    assert s.get_correction_patterns() == []
    assert s.backfill_review_cases() == {"created": 0, "reconciled": 0}
    s.connection.close()


def test_d1_and_d2_triggers_still_enforce_under_d1_code(d2_db):
    s = storage_d1.Storage(d2_db)
    conn = s.connect()
    obs_id = conn.execute("SELECT id FROM classification_observations LIMIT 1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_RAW_HYPOTHESIS"):
        conn.execute("UPDATE classification_observations SET device_id = 'x' WHERE id = ?",
                     (obs_id,))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_OBSERVATION_INPUT"):
        conn.execute("UPDATE classification_observations SET input_id = 'x' WHERE id = ?",
                     (obs_id,))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        conn.execute("DELETE FROM classification_sweeps")
    conn.rollback()
    s.connection.close()


def test_roll_forward_after_d1_activity_keeps_everything(d2_db, monkeypatch):
    """D2 -> rollback to D1 (writes happen) -> D2 again: nothing lost or broken."""
    s1 = storage_d1.Storage(d2_db)
    oid = s1.log_classification_observation({
        "observation_group_id": "g", "device_id": "dev_d1", "classifier_name": "motion_sensor",
        "hypothesis_category": "motion_sensor", "hypothesis_confidence": 0.9,
        "hypothesis_reasoning": "r", "device_name": "n", "device_model": "m",
        "device_manufacturer": "x", "device_source_adapter": "ha", "device_entity_count": 1})
    assert oid
    s1.upsert_review_case("dev_d1", "motion_sensor", "motion_sensor", oid)
    s1.connection.close()
    before = sqlite3.connect(d2_db).execute(
        "SELECT COUNT(*) FROM classification_observations").fetchone()[0]

    s2 = StorageD2(d2_db)                           # roll forward
    result = asyncio.run(ObservationSweep(s2, _classifiers(), "active").run(
        "reconnect", *full_home()))
    assert result.counts["unchanged"] == 4           # gate state survived the rollback
    assert s2.connect().execute(
        "SELECT COUNT(*) FROM classification_observations").fetchone()[0] == before
    assert s2.connect().execute(
        "SELECT input_id FROM classification_observations WHERE id = ?", (oid,)
    ).fetchone()[0] is None
    s2.connection.close()
