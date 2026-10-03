"""scripts/shadow_check.py: S1-S6 computed from stored rows, read-only."""

import asyncio
import hashlib
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.sweep as sweep_module
from src.storage import Storage
from src.sweep import ObservationSweep
from tests.ha_payloads import full_home
from tests.test_d2_verify import dbs  # noqa: F401  (pre.db + live.db with one shadow sweep)
from tests.test_storage import _obs
from tests.test_sweep import _Exploding, _classifiers

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "shadow_check.py"
spec = importlib.util.spec_from_file_location("shadow_check", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

SYNTHETIC = {"classified": 3, "matches": 4, "motion": 2}


@pytest.fixture(autouse=True)
def _step7p_era(monkeypatch):
    """This verifier belongs to the STEP 7P-era database: build the test
    databases without the STEP 7a tables and triggers (which it would,
    correctly, report as unexpected)."""
    monkeypatch.setattr(Storage, "init_step7a_schema", lambda self, cursor: None)
ORIGIN = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _sweep(tmp, source, snapshot=None, classifiers=None, mode="shadow", at_hours=None):
    """at_hours: finished_at of this sweep, in hours after a fixed origin."""
    live = Storage(tmp / "live.db")
    original = sweep_module._now
    if at_hours is not None:
        stamp = (ORIGIN + timedelta(hours=at_hours)).isoformat()
        sweep_module._now = lambda: stamp
    try:
        asyncio.run(ObservationSweep(
            live, _classifiers() if classifiers is None else classifiers, mode).run(
            source, *(snapshot or full_home())))
    finally:
        sweep_module._now = original
        live.connection.close()


def _results(tmp, monkeypatch, gap_hours=0.0):
    monkeypatch.setattr(check, "PROBE", SYNTHETIC)
    monkeypatch.setattr(check, "MIN_S4_GAP_HOURS", gap_hours)
    live = sqlite3.connect(tmp / "live.db")
    pre = sqlite3.connect(tmp / "pre.db")
    sweeps = check._shadow_sweeps(live)
    out = {"S1": check.s1_coverage(sweeps), "S2": check.s2_errors(sweeps),
           "S3": check.s3_probe_parity(sweeps), "S4": check.s4_gate_stability(sweeps),
           "S5": check.s5_missing_metadata(sweeps), "S6": check.s6_no_writes(live, pre)}
    live.close()
    pre.close()
    return {k: v[0] for k, v in out.items()}, out


def _renamed(index=2, name="Renamed"):
    devices, entities, states = full_home()
    devices[index]["name"] = name
    return devices, entities, states


@pytest.fixture
def timed(dbs):
    """pre.db + live.db whose only sweep is a startup sweep at hour 0."""
    shutil.copy(dbs / "pre.db", dbs / "live.db")
    _sweep(dbs, "startup", at_hours=0)
    return dbs


def test_all_met_with_a_stable_window_of_event_sweeps(timed, monkeypatch):
    """No `daily` sweep needed: registry-event sweeps over >= 20 h with other
    state values in between prove the same thing."""
    devices, entities, states = full_home()
    for s in states:
        s["state"] = "changed"
    _sweep(timed, "entity_registry_updated", at_hours=7)
    _sweep(timed, "entity_registry_updated", (devices, entities, states), at_hours=20.5)
    status, detail = _results(timed, monkeypatch, gap_hours=20.0)
    assert status == dict.fromkeys(status, True)
    assert "20.5 h" in detail["S4"][1] and "3 sweeps, 4 devices identical" in detail["S4"][1]


def test_s4_not_met_when_the_window_is_too_short(timed, monkeypatch):
    _sweep(timed, "daily", at_hours=19)
    status, detail = _results(timed, monkeypatch, gap_hours=20.0)
    assert status["S4"] is False and "19.0 h (need 20)" in detail["S4"][1]


def test_s4_not_met_with_a_single_sweep(timed, monkeypatch):
    status, detail = _results(timed, monkeypatch, gap_hours=20.0)
    assert status["S4"] is False and "0.0 h" in detail["S4"][1]


def test_s4_not_met_when_nothing_was_fingerprinted(timed, monkeypatch):
    shutil.copy(timed / "pre.db", timed / "live.db")
    _sweep(timed, "startup", ([], [], []), at_hours=0)
    _sweep(timed, "daily", ([], [], []), at_hours=30)
    status, _ = _results(timed, monkeypatch, gap_hours=20.0)
    assert status["S4"] is False


def test_s4_a_fingerprint_change_breaks_the_window(timed, monkeypatch):
    """A change at hour 10 splits 0..25 h into windows of 0 h and 15 h."""
    _sweep(timed, "entity_registry_updated", _renamed(), at_hours=10)
    _sweep(timed, "entity_registry_updated", _renamed(), at_hours=25)
    status, detail = _results(timed, monkeypatch, gap_hours=20.0)
    assert status["S4"] is False and "15.0 h" in detail["S4"][1]
    assert "fingerprint changed 1 (more than once 0)" in detail["S4"][1]


def test_s4_changes_outside_the_window_are_reported_not_fatal(timed, monkeypatch):
    """Production shape: 20.5 h stable, then a rename, a removal and a second
    rename of the same device. Reported; S4 stays MET; S3 still at parity
    because the renamed socket is re-evaluated to the same result."""
    _sweep(timed, "entity_registry_updated", at_hours=20.5)
    _sweep(timed, "device_registry_updated", _renamed(), at_hours=35)
    devices, entities, states = _renamed(name="Renamed again")
    _sweep(timed, "device_registry_updated", (devices[:-1], entities, states), at_hours=40)
    status, detail = _results(timed, monkeypatch, gap_hours=20.0)
    assert status["S4"] is True and status["S3"] is True
    assert "fingerprint changed 1 (more than once 1)" in detail["S4"][1]
    assert "added 0, removed 1" in detail["S4"][1]
    assert "gate inconsistencies: none" in detail["S4"][1]


def test_s4_window_must_be_consecutive(timed, monkeypatch):
    """Renamed and renamed back: first and last sweep are identical 30 h
    apart, but the fingerprint was not stable throughout."""
    _sweep(timed, "entity_registry_updated", _renamed(), at_hours=10)
    _sweep(timed, "entity_registry_updated", at_hours=12)
    _sweep(timed, "entity_registry_updated", at_hours=30)
    status, detail = _results(timed, monkeypatch, gap_hours=20.0)
    assert status["S4"] is False and "18.0 h" in detail["S4"][1]


def test_s4_error_sweep_breaks_the_window(timed, monkeypatch):
    devices, entities, states = full_home()
    devices[3]["name"] = "Changed app"
    _sweep(timed, "entity_registry_updated", (devices, entities, states), at_hours=10,
           classifiers=dict(_classifiers(), x=_Exploding("dev_app")))
    _sweep(timed, "entity_registry_updated", at_hours=25)
    status, _ = _results(timed, monkeypatch, gap_hours=20.0)
    assert status["S4"] is False and status["S2"] is False


@pytest.mark.parametrize("field, value", [
    ("outcome", "unchanged"),             # claims unchanged, fingerprint differs
    ("baseline_fingerprint", "f" * 64),   # baseline not what the baseline sweep stored
])
def test_s4_detects_inconsistent_gate_decisions(timed, monkeypatch, field, value):
    _sweep(timed, "entity_registry_updated", at_hours=21)
    _sweep(timed, "entity_registry_updated", _renamed(), at_hours=22)
    live = sqlite3.connect(timed / "live.db")
    sweeps = check._shadow_sweeps(live)
    live.close()
    devices = json.loads(sweeps[-1]["devices_json"])
    devices["dev_socket"][field] = value
    sweeps[-1]["devices_json"] = json.dumps(devices)
    monkeypatch.setattr(check, "MIN_S4_GAP_HOURS", 20.0)
    ok, detail = check.s4_gate_stability(sweeps)
    assert ok is False and f"{sweeps[-1]['id']}:dev_socket" in detail


def test_s3_effective_result_must_also_match_the_probe(timed, monkeypatch):
    """A classified device re-evaluated to no_match: the startup sweep still
    shows parity, the effective result does not -> CHECK."""
    _sweep(timed, "entity_registry_updated", _renamed(), at_hours=5, classifiers={})
    status, detail = _results(timed, monkeypatch)
    assert status["S3"] is None and "effective now: {'classified': 2" in detail["S3"][1]


def test_s2_counts_errors(dbs, monkeypatch):
    devices, entities, states = full_home()
    devices[3]["name"] = "Changed app"      # so the gate lets it through
    _sweep(dbs, "daily", (devices, entities, states),
           classifiers=dict(_classifiers(), x=_Exploding("dev_app")))
    status, _ = _results(dbs, monkeypatch)
    assert status["S2"] is False


def test_s3_differs_from_probe_needs_a_decision(dbs, monkeypatch):
    monkeypatch.setattr(check, "PROBE", {"classified": 18, "matches": 20, "motion": 4})
    live = sqlite3.connect(dbs / "live.db")
    ok, detail = check.s3_probe_parity(check._shadow_sweeps(live))
    live.close()
    assert ok is None and "explain differences" in detail


def test_s5_missing_metadata_above_limit(dbs, monkeypatch):
    devices, entities, states = full_home()
    states = [s for s in states if s["entity_id"] != "sensor.desk_lamp_power"]
    _sweep(dbs, "daily", (devices, entities, states))
    status, detail = _results(dbs, monkeypatch)
    assert status["S5"] is False and "dev_socket" in detail["S5"][1]


def test_s6_detects_writes(dbs, monkeypatch):
    live = Storage(dbs / "live.db")
    _obs(live, "g9", "dev9", "env", "environmental")
    live.connection.close()
    status, _ = _results(dbs, monkeypatch)
    assert status["S6"] is False


def test_script_is_read_only_and_prints_no_names(dbs):
    before = hashlib.sha256((dbs / "live.db").read_bytes()).hexdigest()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--live-db", str(dbs / "live.db"),
         "--pre-db", str(dbs / "pre.db")], capture_output=True, text=True)
    assert "S1 coverage" in result.stdout and "RESULT:" in result.stdout
    assert result.returncode == 1          # no 20 h window yet -> NOT READY
    assert "Desk lamp" not in result.stdout
    assert hashlib.sha256((dbs / "live.db").read_bytes()).hexdigest() == before
