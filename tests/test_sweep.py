"""STEP 7P gated sweep (ADR 6-8): T4-T9, T11, T12 and the shadow baseline."""

import asyncio
import copy
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.classifiers.energy import EnergyMeterClassifier
from src.classifiers.environmental import EnvironmentalSensorClassifier
from src.classifiers.motion import MotionSensorClassifier
from src.coverage_store import get_input
from src.observation_input import fingerprint
from src.storage import Storage
from src.sweep import ObservationSweep
from tests.ha_payloads import full_home, home, motion_only, smart_socket
from tests.test_storage import _obs


def _classifiers():
    return {"energy_meter": EnergyMeterClassifier(),
            "environmental_sensor": EnvironmentalSensorClassifier(),
            "motion_sensor": MotionSensorClassifier()}


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as tmp:
        s = Storage(Path(tmp) / "core.db")
        yield s
        s.connection.close()


def _run(storage, mode, snapshot=None, source="startup", classifiers=None):
    sweep = ObservationSweep(storage, classifiers or _classifiers(), mode)
    devices, entities, states = snapshot or full_home()
    return asyncio.run(sweep.run(source, devices, entities, states))


def _count(storage, table):
    return storage.connect().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _sweeps(storage):
    rows = storage.connect().execute(
        "SELECT * FROM classification_sweeps ORDER BY rowid").fetchall()
    return [dict(r) for r in rows]


# --- T6: coverage invariant; outcomes per archetype ----------------------------

def test_every_device_has_exactly_one_outcome(storage):
    result = _run(storage, "active")
    c = result.counts
    assert c["discovered"] == 5
    assert (c["skipped_no_entities"] + c["skipped_missing_metadata"] + c["unchanged"]
            + c["classified"] + c["no_match"] + c["error"]) == c["discovered"]
    outcomes = {d: e["outcome"] for d, e in result.devices.items()}
    assert outcomes == {"dev_motion": "classified", "dev_multi": "classified",
                        "dev_socket": "classified", "dev_app": "no_match",
                        "dev_empty": "skipped"}
    assert result.devices["dev_multi"]["matched_classifiers"] == [
        "environmental_sensor", "motion_sensor"]
    assert result.devices["dev_empty"]["reason"] == "no_entities"


# --- active mode writes --------------------------------------------------------

def test_active_writes_inputs_observations_and_review_cases(storage):
    result = _run(storage, "active")
    assert _count(storage, "classification_inputs") == 4       # not the skipped one
    assert _count(storage, "classification_observations") == 4  # motion, env+motion, energy
    assert result.observations_written == 4
    assert _count(storage, "review_cases") == 4
    obs = storage.connect().execute(
        "SELECT input_id, device_source_adapter, device_entity_count "
        "FROM classification_observations").fetchall()
    assert all(r["input_id"] and r["device_source_adapter"] == "ha" for r in obs)


# --- T7: no_match -----------------------------------------------------------------

def test_no_match_writes_input_but_no_observation(storage):
    result = _run(storage, "active")
    entry = result.devices["dev_app"]
    record = get_input(storage.connect(), entry["input_id"])
    assert record["outcome"] == "no_match"
    assert record["matched_classifiers"] == []
    assert storage.connect().execute(
        "SELECT COUNT(*) FROM classification_observations WHERE device_id = 'dev_app'"
    ).fetchone()[0] == 0


# --- T5: gate -------------------------------------------------------------------

def test_second_sweep_without_structural_change_writes_nothing(storage):
    _run(storage, "active")
    before = {t: _count(storage, t) for t in
              ("classification_inputs", "classification_observations", "review_cases")}
    cases_before = [dict(r) for r in storage.connect().execute(
        "SELECT * FROM review_cases ORDER BY id")]

    devices, entities, states = full_home()
    for s in states:                      # thousands of state changes, in effect
        s["state"] = "changed"
        s["last_changed"] = "2031-01-01T00:00:00+00:00"
    result = _run(storage, "active", (devices, entities, states), source="daily")

    assert result.counts["unchanged"] == 4
    assert result.counts["classified"] == result.counts["no_match"] == 0
    assert {t: _count(storage, t) for t in before} == before
    assert [dict(r) for r in storage.connect().execute(
        "SELECT * FROM review_cases ORDER BY id")] == cases_before


def test_structural_change_produces_exactly_one_new_input(storage):
    _run(storage, "active")
    devices, entities, states = full_home()
    entities.append({"entity_id": "sensor.desk_lamp_voltage", "device_id": "dev_socket",
                     "platform": "tuya", "name": None, "original_name": "Voltage",
                     "disabled_by": None})
    states.append({"entity_id": "sensor.desk_lamp_voltage", "state": "230",
                   "attributes": {"device_class": "voltage"}})
    result = _run(storage, "active", (devices, entities, states),
                  source="entity_registry_updated")
    assert result.counts["classified"] == 1 and result.counts["unchanged"] == 3
    assert storage.connect().execute(
        "SELECT COUNT(*) FROM classification_inputs WHERE device_id = 'dev_socket'"
    ).fetchone()[0] == 2


def test_version_bump_reclassifies_without_reopening_resolved_cases(storage):
    _run(storage, "active")
    case = storage.connect().execute(
        "SELECT id, last_observation_id FROM review_cases "
        "WHERE device_id = 'dev_socket'").fetchone()
    assert storage.resolve_review_case(case["id"], case["last_observation_id"],
                                       decision="approved") == "resolved"
    sweep = ObservationSweep(storage, _classifiers(), "active", classifier_set_version="2")
    result = asyncio.run(sweep.run("startup", *full_home()))
    assert result.counts["classified"] == 3 and result.counts["no_match"] == 1
    row = storage.connect().execute(
        "SELECT status, decision FROM review_cases WHERE id = ?", (case["id"],)).fetchone()
    assert (row["status"], row["decision"]) == ("resolved", "approved")


# --- T4: missing metadata at sweep level ----------------------------------------

def test_missing_metadata_is_skipped_not_no_match_and_retried(storage):
    devices, entities, states = home(motion_only, smart_socket)
    partial = [s for s in states if s["entity_id"] != "binary_sensor.hall_presence"]
    first = _run(storage, "active", (devices, entities, partial))
    entry = first.devices["dev_motion"]
    assert (entry["outcome"], entry["reason"]) == ("skipped", "missing_metadata")
    assert first.counts["skipped_missing_metadata"] == 1 and first.counts["no_match"] == 0
    assert storage.connect().execute(
        "SELECT COUNT(*) FROM classification_inputs WHERE device_id = 'dev_motion'"
    ).fetchone()[0] == 0

    second = _run(storage, "active", (devices, entities, states), source="daily")
    assert second.devices["dev_motion"]["outcome"] == "classified"
    assert second.devices["dev_motion"]["matched_classifiers"] == ["motion_sensor"]


# --- T8: errors -------------------------------------------------------------------

class _Exploding:
    def __init__(self, device_id):
        self.device_id = device_id

    async def classify(self, device):
        if device["id"] == self.device_id:
            raise ValueError("boom")
        return None


def test_classifier_error_is_isolated_and_does_not_advance_gate(storage):
    classifiers = dict(_classifiers(), broken=_Exploding("dev_socket"))
    first = _run(storage, "active", classifiers=classifiers)
    assert first.devices["dev_socket"]["outcome"] == "error"
    assert first.devices["dev_socket"]["reason"] == "ValueError"
    assert first.counts["error"] == 1 and first.counts["classified"] == 2
    assert storage.connect().execute(
        "SELECT COUNT(*) FROM classification_inputs WHERE device_id = 'dev_socket'"
    ).fetchone()[0] == 0

    second = _run(storage, "active", source="daily")  # classifier fixed
    assert second.devices["dev_socket"]["outcome"] == "classified"
    assert second.counts["unchanged"] == 3


def test_input_and_observations_are_atomic(storage, monkeypatch):
    original = Storage._insert_observation
    calls = {"n": 0}

    def fail_second(cursor, payload, now):
        calls["n"] += 1
        if payload["device_id"] == "dev_multi" and calls["n"] % 2 == 0:
            raise sqlite3.OperationalError("disk full")
        return original(cursor, payload, now)

    monkeypatch.setattr(Storage, "_insert_observation", staticmethod(fail_second))
    result = _run(storage, "active")
    assert result.devices["dev_multi"]["outcome"] == "error"
    for table in ("classification_inputs", "classification_observations"):
        assert storage.connect().execute(
            f"SELECT COUNT(*) FROM {table} WHERE device_id = 'dev_multi'").fetchone()[0] == 0


def test_classifiers_cannot_alter_the_stored_snapshot(storage):
    class Mutating:
        async def classify(self, device):
            device["entities"].clear()
            device["name"] = "tampered"
            return None

    result = _run(storage, "active", classifiers=dict(_classifiers(), zz=Mutating()))
    record = get_input(storage.connect(), result.devices["dev_socket"]["input_id"])
    assert record["snapshot"]["name"] == "Desk lamp"
    assert len(record["snapshot"]["entities"]) == 3


# --- T9: shadow writes only the sweep row ----------------------------------------

def test_shadow_writes_only_the_sweep_row(storage):
    _obs(storage, "g0", "dev_old", "env", "environmental")  # pre-existing history
    before = {t: _count(storage, t) for t in
              ("classification_inputs", "classification_observations", "review_cases")}
    result = _run(storage, "shadow")
    assert {t: _count(storage, t) for t in before} == before
    assert _count(storage, "classification_sweeps") == 1
    assert result.observations_written == 0
    row = _sweeps(storage)[0]
    assert row["mode"] == "shadow" and row["baseline_sweep_id"] is None
    devices = json.loads(row["devices_json"])
    assert set(devices) == {"dev_motion", "dev_multi", "dev_socket", "dev_app", "dev_empty"}
    assert devices["dev_multi"]["matched_classifiers"] == ["environmental_sensor",
                                                          "motion_sensor"]
    assert devices["dev_empty"]["fingerprint"] is None
    assert all(len(e["fingerprint"]) == 64 for d, e in devices.items() if d != "dev_empty")


def test_shadow_baseline_is_previous_shadow_sweep_and_reproducible(storage):
    first = _run(storage, "shadow")
    devices, entities, states = full_home()
    for s in states:
        s["state"] = "changed"
    second = _run(storage, "shadow", (devices, entities, states), source="daily")

    rows = _sweeps(storage)
    assert rows[1]["baseline_sweep_id"] == first.sweep_id
    assert second.counts["unchanged"] == 4
    # S4 re-checked purely from stored rows: fingerprint == baseline_fingerprint
    stored_first = json.loads(rows[0]["devices_json"])
    stored_second = json.loads(rows[1]["devices_json"])
    for device_id, entry in stored_second.items():
        if entry["outcome"] == "unchanged":
            assert entry["fingerprint"] == entry["baseline_fingerprint"] \
                == stored_first[device_id]["fingerprint"]


def test_shadow_error_does_not_become_baseline(storage):
    _run(storage, "shadow", classifiers=dict(_classifiers(), b=_Exploding("dev_socket")))
    second = _run(storage, "shadow", source="daily")
    assert second.devices["dev_socket"]["outcome"] == "classified"
    assert second.devices["dev_socket"]["baseline_fingerprint"] is None


def test_first_active_sweep_after_shadow_attempts_every_device(storage):
    _run(storage, "shadow")
    _run(storage, "shadow", source="daily")
    active = _run(storage, "active", source="startup")
    assert active.counts["unchanged"] == 0
    assert active.counts["classified"] == 3 and active.counts["no_match"] == 1
    assert _sweeps(storage)[-1]["baseline_sweep_id"] is None


def test_unknown_mode_is_rejected(storage):
    with pytest.raises(ValueError):
        ObservationSweep(storage, _classifiers(), "live")


# --- T11: immutability of the new data --------------------------------------------

@pytest.mark.parametrize("sql, message", [
    ("UPDATE classification_inputs SET outcome = 'no_match'",
     "APPEND_ONLY: classification_inputs rows cannot be updated"),
    ("DELETE FROM classification_inputs",
     "APPEND_ONLY: classification_inputs rows cannot be deleted"),
    ("UPDATE classification_sweeps SET error = 99",
     "APPEND_ONLY: classification_sweeps rows cannot be updated"),
    ("DELETE FROM classification_sweeps",
     "APPEND_ONLY: classification_sweeps rows cannot be deleted"),
    ("UPDATE classification_observations SET input_id = 'inp_other'",
     "IMMUTABLE_OBSERVATION_INPUT: classification_observations.input_id "
     "cannot be changed after insert"),
])
def test_new_data_is_protected(storage, sql, message):
    _run(storage, "active")
    with pytest.raises(sqlite3.IntegrityError, match=message):
        storage.connect().execute(sql)


def test_legacy_rows_keep_null_input_id_and_cannot_be_backfilled(storage):
    old = _obs(storage, "g0", "dev_old", "env", "environmental")
    conn = storage.connect()
    assert conn.execute("SELECT input_id FROM classification_observations WHERE id = ?",
                        (old,)).fetchone()[0] is None
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_OBSERVATION_INPUT"):
        conn.execute("UPDATE classification_observations SET input_id = 'inp_x' "
                     "WHERE id = ?", (old,))
    conn.execute("UPDATE classification_observations SET input_id = input_id")  # no change


def _data_rows(storage):
    """INSERT lines of the dump, without review_cases (derived state that
    the ordinary startup backfill maintains - not part of the 7P upgrade)."""
    return [line for line in storage.connect().iterdump()
            if line.startswith("INSERT") and '"review_cases"' not in line]


def test_upgrading_a_d1_database_changes_no_existing_row():
    """D2 upgrade path: a D1-era database gains the 7P schema on startup."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "core.db"
        original = Storage.init_step7p_schema
        Storage.init_step7p_schema = lambda self, cursor: None
        try:
            s = Storage(path)
            _obs(s, "g1", "dev1", "env", "environmental")
            before = _data_rows(s)
            s.connection.close()
        finally:
            Storage.init_step7p_schema = original
        upgraded = Storage(path)
        after = _data_rows(upgraded)
        cols = [r[1] for r in upgraded.connect().execute(
            "PRAGMA table_info(classification_observations)")]
        upgraded.connection.close()
    assert "input_id" in cols
    # the only difference: each existing observation row gains a NULL input_id
    assert [l.replace(",NULL);", ");") if "classification_observations" in l else l
            for l in after] == before


# --- T12: offline replay -----------------------------------------------------------

def test_observation_replays_from_its_stored_input(storage):
    _run(storage, "active")
    conn = storage.connect()
    for obs in conn.execute("SELECT * FROM classification_observations").fetchall():
        record = get_input(conn, obs["input_id"])
        assert fingerprint(record["snapshot"], record["classifier_set_version"]) \
            == record["fingerprint"]
        result = asyncio.run(_classifiers()[obs["classifier_name"]].classify(
            copy.deepcopy(record["snapshot"])))
        assert result["category"] == obs["hypothesis_category"]
        assert result["confidence"] == obs["hypothesis_confidence"]
        assert result["reasoning"] == obs["hypothesis_reasoning"]
