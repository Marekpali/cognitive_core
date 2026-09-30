"""scripts/shadow_check.py: S1-S6 computed from stored rows, read-only."""

import asyncio
import hashlib
import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

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


def _sweep(tmp, source, snapshot=None, classifiers=None, mode="shadow"):
    live = Storage(tmp / "live.db")
    asyncio.run(ObservationSweep(live, classifiers or _classifiers(), mode).run(
        source, *(snapshot or full_home())))
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


def test_all_met_after_startup_and_stable_daily(dbs, monkeypatch):
    devices, entities, states = full_home()
    for s in states:
        s["state"] = "changed"
    _sweep(dbs, "daily", (devices, entities, states))
    status, _ = _results(dbs, monkeypatch)
    assert status == dict.fromkeys(status, True)


def test_s4_not_met_when_daily_follows_its_baseline_too_soon(dbs, monkeypatch):
    _sweep(dbs, "daily")                  # seconds after startup, not ~24 h
    status, detail = _results(dbs, monkeypatch, gap_hours=20.0)
    assert status["S4"] is False and "20 h" in detail["S4"][1]


def test_s4_not_met_when_nothing_was_gated(dbs, monkeypatch):
    _sweep(dbs, "daily", ([], [], []))
    status, _ = _results(dbs, monkeypatch)
    assert status["S4"] is False


def test_s4_not_met_before_first_daily(dbs, monkeypatch):
    status, detail = _results(dbs, monkeypatch)
    assert status["S4"] is False and "no daily" in detail["S4"][1]


def test_s4_not_met_when_fingerprints_move(dbs, monkeypatch):
    devices, entities, states = full_home()
    devices[2]["name"] = "Renamed"
    _sweep(dbs, "daily", (devices, entities, states))
    status, detail = _results(dbs, monkeypatch)
    assert status["S4"] is False and "dev_socket" in detail["S4"][1]


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
    assert result.returncode == 1          # no daily sweep yet -> NOT READY
    assert "Desk lamp" not in result.stdout
    assert hashlib.sha256((dbs / "live.db").read_bytes()).hexdigest() == before
