"""scripts/active_verify.py - activation check: cumulative D0 + D1 + D2 and
the active chain sweep -> input -> observation -> review case. Read-only."""

import asyncio
import hashlib
import importlib.metadata
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.storage import Storage
from src.sweep import ObservationSweep
from tests.ha_payloads import full_home
from tests.test_d2_verify import _d1_era
from tests.test_m0_precheck import DEVICE_NAME, _build_db
from tests.test_storage import _obs
from tests.test_sweep import _classifiers

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "active_verify.py"
spec = importlib.util.spec_from_file_location("active_verify", SCRIPT)
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)

ALL = ("D0", "D1", "D2", "PINS", "DATA", "ACTIVE", "REPEAT")


def _sweep(path: Path, mode: str, snapshot=None, source="startup"):
    storage = Storage(path)
    result = asyncio.run(ObservationSweep(storage, _classifiers(), mode).run(
        source, *(snapshot or full_home())))
    storage.connection.close()
    return result


def _motion_category(tmp: Path) -> str:
    _sweep(tmp / "scratch.db", "active")
    conn = sqlite3.connect(tmp / "scratch.db")
    category = conn.execute(
        "SELECT hypothesis_category FROM classification_observations "
        "WHERE device_id = 'dev_motion' AND classifier_name = 'motion_sensor'").fetchone()[0]
    conn.close()
    return category


@pytest.fixture
def dbs(tmp_path):
    """pre_active.db: D1-era history (one resolved, one pending case), then
    the STEP 7P migration, a human decision on the very hypothesis the
    first active sweep will observe again (dev_motion), and a shadow sweep.
    live.db starts as a copy of it."""
    category = _motion_category(tmp_path)
    live = tmp_path / "live.db"
    _d1_era(lambda: _build_db(live).connection.close())
    storage = Storage(live)
    old = _obs(storage, "g3", "dev_motion", "motion_sensor", category, name=DEVICE_NAME)
    case = storage.upsert_review_case("dev_motion", "motion_sensor", category, old)
    storage.resolve_review_case(case, expected_observation_id=old, decision="approved")
    storage.connection.close()
    _sweep(live, "shadow")
    shutil.copy(live, tmp_path / "pre_active.db")
    (tmp_path / "work").mkdir()
    (tmp_path / "requirements.txt").write_text(
        f"pytest=={importlib.metadata.version('pytest')}\n", encoding="utf-8")
    (tmp_path / "options.json").write_text('{"observation_mode": "active"}', encoding="utf-8")
    return tmp_path


def _run(tmp: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--src-root", str(REPO),
         "--live-db", str(tmp / "live.db"), "--pre-db", str(tmp / "pre_active.db"),
         "--work-dir", str(tmp / "work"), "--requirements", str(tmp / "requirements.txt"),
         "--options", str(tmp / "options.json")],
        capture_output=True, text=True)


def _sections(stdout: str) -> dict:
    return {line.split()[0]: line.split()[1] for line in stdout.splitlines()
            if len(line.split()) == 2 and line.split()[0] in ALL}


def _sql(tmp: Path, *statements):
    conn = sqlite3.connect(tmp / "live.db")
    for sql in statements:
        conn.execute(sql)
    conn.commit()
    conn.close()


def _renamed():
    devices, entities, states = full_home()
    devices[2]["name"] = "Renamed"
    return devices, entities, states


def test_negative_control_before_activation(dbs):
    """Still shadow: everything inherited passes, only ACTIVE fails."""
    result = _run(dbs)
    assert result.returncode == 1
    assert _sections(result.stdout) == {
        "D0": "PASS", "D1": "PASS", "D2": "PASS", "PINS": "PASS", "DATA": "PASS",
        "ACTIVE": "FAIL", "REPEAT": "PENDING"}
    assert "no active sweep since the snapshot" in result.stdout


def test_first_active_sweep_passes_and_repeat_is_pending(dbs):
    _sweep(dbs / "live.db", "active")
    before = hashlib.sha256((dbs / "live.db").read_bytes()).hexdigest()
    result = _run(dbs)
    assert result.returncode == 2, result.stdout + result.stderr
    assert _sections(result.stdout) == {
        **dict.fromkeys(("D0", "D1", "D2", "PINS", "DATA", "ACTIVE"), "PASS"),
        "REPEAT": "PENDING"}
    for expected in (
            "classification_inputs: 0 in snapshot, 4 live (+4)",
            "classification_observations: 3 in snapshot, 7 live (+4)",
            "evaluated 4 (classified 3), unchanged 0, error 0, inputs written 4",
            "new observations: 4; expected from new inputs: 4",
            # dev_motion's hypothesis already had a (resolved) case -> 3 new
            "review cases: 3 new (all must be pending), 1 existing advanced to new evidence",
            "resolved in snapshot: 2; still resolved with identical decision fields: 2",
            "resolved cases re-decided on a copy: 2",
            "RESULT: INCOMPLETE - second active sweep pending"):
        assert expected in result.stdout, expected
    assert DEVICE_NAME not in result.stdout and "Desk lamp" not in result.stdout
    assert hashlib.sha256((dbs / "live.db").read_bytes()).hexdigest() == before


def test_second_sweep_writes_nothing_and_completes(dbs):
    _sweep(dbs / "live.db", "active")
    _sweep(dbs / "live.db", "active", source="entity_registry_updated")
    result = _run(dbs)
    assert result.returncode == 0, result.stdout
    assert _sections(result.stdout) == dict.fromkeys(ALL, "PASS")
    assert "later active sweeps: 1; with nothing evaluated or written: 1" in result.stdout
    assert "classification_inputs: 0 in snapshot, 4 live (+4)" in result.stdout
    assert "RESULT: PASS" in result.stdout


def test_later_sweep_with_a_real_registry_change_is_consistent(dbs):
    _sweep(dbs / "live.db", "active")
    _sweep(dbs / "live.db", "active", _renamed(), source="device_registry_updated")
    result = _run(dbs)
    assert result.returncode == 0, result.stdout
    assert "evaluated 1 (classified 1), unchanged 3, error 0, inputs written 1" in result.stdout
    assert "with nothing evaluated or written: 0" in result.stdout
    assert "new observations: 5; expected from new inputs: 5" in result.stdout


@pytest.mark.parametrize("statements, expected", [
    (["DELETE FROM classification_observations WHERE id = (SELECT id FROM "
      "classification_observations WHERE input_id IS NOT NULL LIMIT 1)"],
     "observations do not equal matched_classifiers"),
    (["DROP TRIGGER trg_classification_inputs_no_delete",
      "DELETE FROM classification_inputs WHERE device_id = 'dev_app'",
      "CREATE TRIGGER trg_classification_inputs_no_delete BEFORE DELETE ON "
      "classification_inputs BEGIN SELECT RAISE(ABORT, "
      "'APPEND_ONLY: classification_inputs rows cannot be deleted'); END"],
     "dev_app: no classification_inputs row"),
    (["INSERT INTO classification_inputs VALUES "
      "('inp_orphan','dev_x','f','1','{}','no_match','[]','swp_gone','t')"],
     "inp_orphan: input of unknown sweep swp_gone"),
    (["DELETE FROM review_cases WHERE device_id = 'dev_socket'"],
     "no review case for a new observation of dev_socket/energy_meter"),
    (["UPDATE review_cases SET status = 'pending', decision = NULL, decided_at = NULL "
      "WHERE device_id = 'dev_motion'"],
     "resolved case reopened or decision changed"),
    (["UPDATE review_cases SET status = 'resolved', decision = 'approved' "
      "WHERE device_id = 'dev_socket'"],
     "new case is not plain pending"),
    (["UPDATE classification_observations SET human_decision = 'approved' "
      "WHERE device_id = 'dev_socket'"],
     "new observation already carries a decision"),
    (["INSERT INTO assets (id, name, hypothesis_category, hypothesis_confidence, "
      "lifecycle_discovered_at, device_data, created_at, updated_at) "
      "VALUES ('a', 'n', 'c', 0.5, 't', '{}', 't', 't')"],
     "assets changed"),
])
def test_broken_chain_fails_active(dbs, statements, expected):
    _sweep(dbs / "live.db", "active")
    _sql(dbs, *statements)
    result = _run(dbs)
    assert result.returncode == 1
    assert _sections(result.stdout)["ACTIVE"] == "FAIL"
    assert expected in result.stdout


def test_observation_without_input_fails_active(dbs):
    _sweep(dbs / "live.db", "active")
    storage = Storage(dbs / "live.db")
    _obs(storage, "g9", "dev9", "env", "environmental")
    storage.connection.close()
    result = _run(dbs)
    assert _sections(result.stdout)["ACTIVE"] == "FAIL"
    assert "new observation without a new input" in result.stdout


def test_shadow_sweep_after_activation_fails_active(dbs):
    _sweep(dbs / "live.db", "active")
    _sweep(dbs / "live.db", "shadow", source="entity_registry_updated")
    result = _run(dbs)
    assert _sections(result.stdout)["ACTIVE"] == "FAIL"
    assert "shadow sweep after activation" in result.stdout


def _active_sweeps(tmp: Path) -> tuple:
    conn = sqlite3.connect(tmp / "live.db")
    sweeps = [s for s in verify._dicts(
        conn, "SELECT * FROM classification_sweeps ORDER BY rowid") if s["mode"] == "active"]
    inputs = {r["id"]: dict(r, matched_classifiers=json.loads(r["matched_classifiers"]))
              for r in verify._dicts(conn, "SELECT id, device_id, fingerprint, outcome, "
                                           "matched_classifiers, sweep_id "
                                           "FROM classification_inputs")}
    conn.close()
    return sweeps, inputs


def _replay(sweeps, inputs) -> list:
    latest = {}
    return [verify._replay_sweep(s, latest, inputs) for s in sweeps]


def test_replay_accepts_the_stored_sweeps(dbs):
    _sweep(dbs / "live.db", "active")
    _sweep(dbs / "live.db", "active", _renamed())
    assert _replay(*_active_sweeps(dbs)) == [[], []]


@pytest.mark.parametrize("change, expected", [
    # the renamed device is recorded as `unchanged` although its fingerprint moved
    ({"outcome": "unchanged", "input_id": None}, "unchanged without an equal latest input"),
    ({"baseline_fingerprint": "f" * 64}, "baseline is not the latest input fingerprint"),
    ({"outcome": "error", "reason": "OperationalError"}, "outcome error (OperationalError)"),
    ({"matched_classifiers": []}, "does not match the sweep entry"),
])
def test_replay_detects_inconsistent_later_sweep(dbs, change, expected):
    _sweep(dbs / "live.db", "active")
    _sweep(dbs / "live.db", "active", _renamed())
    sweeps, inputs = _active_sweeps(dbs)
    entries = json.loads(sweeps[1]["devices_json"])
    entries["dev_socket"].update(change)
    sweeps[1]["devices_json"] = json.dumps(entries)
    first, second = _replay(sweeps, inputs)
    assert first == [] and any(expected in f for f in second), second


def test_replay_detects_re_evaluation_of_an_unchanged_device(dbs):
    """A device evaluated again although its fingerprint equals its latest
    input: the gate did not hold."""
    _sweep(dbs / "live.db", "active")
    _sweep(dbs / "live.db", "active", _renamed())
    sweeps, inputs = _active_sweeps(dbs)
    entries = json.loads(sweeps[1]["devices_json"])
    entries["dev_app"].update(outcome="no_match")
    sweeps[1]["devices_json"] = json.dumps(entries)
    _, second = _replay(sweeps, inputs)
    assert any("evaluated although the fingerprint was unchanged" in f for f in second)
    assert any("counts do not match devices_json" in f for f in second)
