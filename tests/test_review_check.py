"""scripts/review_check.py - the chain human decision -> evidence -> blind
next decision, checked against a pre-review snapshot."""

import hashlib
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src import precedent_store
from src.storage import Storage
from tests.precedent_helpers import decided, pending, start_layer

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "review_check.py"
ALL = ("LAYER", "DECISIONS", "EVIDENCE", "UNTOUCHED")
MOTION = dict(classifier="motion_sensor", category="motion_sensor")
DEVICE_NAME = "Secret Hallway Sensor"


@pytest.fixture
def dbs(tmp_path):
    """Before the layer: one resolved environmental case, two pending motion
    cases and one pending environmental case. Then the layer starts;
    pre_review.db is taken there."""
    storage = Storage(tmp_path / "live.db", precedent_mode="off")
    decided(storage, "dev_env0")
    cases = {"m1": pending(storage, "dev_m1", **MOTION), "m2": pending(storage, "dev_m2", **MOTION),
             "m3": pending(storage, "dev_m3", **MOTION), "e1": pending(storage, "dev_env1")}
    storage.connect().execute("UPDATE classification_observations SET review_reason = NULL")
    storage.connection.commit()
    storage.connection.close()
    start_layer(tmp_path / "live.db").connection.close()
    shutil.copy(tmp_path / "live.db", tmp_path / "pre_review.db")
    return tmp_path, cases


def _resolve(tmp: Path, case, decision="approved", **kwargs) -> None:
    storage = Storage(tmp / "live.db", precedent_mode="shadow")
    assert storage.resolve_review_case(*case, decision, **kwargs) == "resolved"
    storage.connection.close()


def _run(tmp: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "--live-db", str(tmp / "live.db"),
                           "--pre-db", str(tmp / "pre_review.db")], capture_output=True, text=True)


def _sections(stdout: str) -> dict:
    return {line.split()[0]: line.split()[1] for line in stdout.splitlines()
            if len(line.split()) == 2 and line.split()[0] in ALL}


def test_canary_two_decisions_first_without_evidence_second_with_n_1(dbs):
    tmp, cases = dbs
    _resolve(tmp, cases["m1"], "rejected")
    _resolve(tmp, cases["m2"], "corrected", corrected_category="occupancy")
    before = hashlib.sha256((tmp / "live.db").read_bytes()).hexdigest()
    result = _run(tmp)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _sections(result.stdout) == dict.fromkeys(ALL, "PASS")
    out = result.stdout
    for expected in (
            "layer_started rows: 1 (cut-off ", "annotation_failed: 0 (0 since the snapshot)",
            "cases resolved since the snapshot: 2",
            f"#1 {cases['m1'][0]} motion_sensor/motion_sensor device dev_m1: rejected at",
            f"#2 {cases['m2'][0]} motion_sensor/motion_sensor device dev_m2: corrected:occupancy at",
            f"#1 {cases['m1'][0]}: no annotation; earlier decisions on other devices: 0",
            "class_pattern bootstrap_pending -> rejected, evidence 1/1 insufficient n=1",
            f"evidence: {cases['m1'][0]} obs {cases['m1'][1]} device dev_m1 rejected at",
            "review_cases: snapshot [('pending', 4), ('resolved', 1)], "
            "live [('pending', 2), ('resolved', 3)]",
            "precedent_annotations: 1", "precedent_annotation_evidence: 1", "RESULT: PASS"):
        assert expected in out, expected
    assert out.count("       evidence: ") == 1
    assert hashlib.sha256((tmp / "live.db").read_bytes()).hexdigest() == before


def test_nothing_decided_yet_passes_and_says_so(dbs):
    tmp, _ = dbs
    result = _run(tmp)
    assert result.returncode == 0 and "cases resolved since the snapshot: 0" in result.stdout


def test_evidence_from_before_the_layer_is_used(dbs):
    tmp, cases = dbs
    _resolve(tmp, cases["e1"])
    result = _run(tmp)
    assert result.returncode == 0, result.stdout
    assert "-> approved, evidence 1/1 insufficient n=1" in result.stdout
    assert "device dev_env0 approved at" in result.stdout


def test_missing_annotation_although_evidence_existed_fails(dbs, monkeypatch):
    tmp, cases = dbs
    _resolve(tmp, cases["m1"])
    monkeypatch.setattr(precedent_store, "labelled_decisions", lambda *a, **k: [])
    _resolve(tmp, cases["m2"])
    result = _run(tmp)
    assert _sections(result.stdout)["EVIDENCE"] == "FAIL"
    assert "1 earlier decision(s) existed but no annotation was written" in result.stdout


def test_failed_annotation_fails_layer(dbs, monkeypatch):
    tmp, cases = dbs
    _resolve(tmp, cases["m1"])

    def boom(*args, **kwargs):
        raise RuntimeError("broken")
    monkeypatch.setattr(precedent_store, "insert_annotation", boom)
    _resolve(tmp, cases["m2"])
    result = _run(tmp)
    assert _sections(result.stdout) == {"LAYER": "FAIL", "DECISIONS": "PASS",
                                        "EVIDENCE": "FAIL", "UNTOUCHED": "PASS"}
    assert "1 annotation_failed row(s) since the snapshot" in result.stdout


def test_incomplete_evidence_fails(dbs, monkeypatch):
    """The annotator saw only part of the earlier decisions."""
    tmp, cases = dbs
    _resolve(tmp, cases["m1"])
    _resolve(tmp, cases["m2"])
    original = precedent_store.labelled_decisions
    monkeypatch.setattr(precedent_store, "labelled_decisions",
                        lambda *a, **k: original(*a, **k)[:1])
    _resolve(tmp, cases["m3"])
    result = _run(tmp)
    assert _sections(result.stdout)["EVIDENCE"] == "FAIL"
    assert "evidence is not exactly the earlier decisions on other devices" in result.stdout


def test_observation_newer_than_the_cut_off_is_judged_at_its_own_time(dbs):
    """Observed after the cut-off with no evidence yet: no annotation then,
    and none at decision time - evidence that appeared later does not count."""
    tmp, cases = dbs
    storage = Storage(tmp / "live.db", precedent_mode="shadow")
    late = pending(storage, "dev_m9", **MOTION)
    storage.connection.close()
    shutil.copy(tmp / "live.db", tmp / "pre_review.db")
    _resolve(tmp, cases["m1"])
    _resolve(tmp, late)
    result = _run(tmp)
    assert result.returncode == 0, result.stdout
    assert f"{late[0]}: no annotation; earlier decisions on other devices: 0" in result.stdout


def test_changed_old_decision_and_new_rows_fail_untouched(dbs):
    tmp, cases = dbs
    storage = Storage(tmp / "live.db", precedent_mode="shadow")
    pending(storage, "dev_new")
    storage.connection.close()
    conn = sqlite3.connect(tmp / "live.db")
    conn.execute("UPDATE review_cases SET decision = 'rejected' WHERE device_id = 'dev_env0'")
    conn.commit()
    conn.close()
    result = _run(tmp)
    assert _sections(result.stdout)["UNTOUCHED"] == "FAIL"
    assert "resolved in the snapshot, now different" in result.stdout
    assert "1 new review_cases row(s) since the snapshot" in result.stdout


def test_output_never_contains_a_device_name(dbs):
    tmp, cases = dbs
    conn = sqlite3.connect(tmp / "live.db")
    names = {r[0] for r in conn.execute("SELECT device_name FROM classification_observations")}
    conn.close()
    _resolve(tmp, cases["m1"])
    _resolve(tmp, cases["m2"])
    out = _run(tmp).stdout
    assert names and not [n for n in names if n and n in out]


def test_missing_snapshot_is_a_clear_message(dbs):
    tmp, _ = dbs
    (tmp / "pre_review.db").unlink()
    result = _run(tmp)
    assert result.returncode == 1 and "RESULT: FAIL - snapshot" in result.stdout
