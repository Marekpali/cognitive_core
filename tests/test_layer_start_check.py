"""scripts/layer_start_check.py - the 7a enable gate: one cut-off, written
before the first startup sweep of the shadow process, and nothing else."""

import hashlib
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.storage import Storage
from tests.precedent_helpers import pending, start_layer, sweep

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "layer_start_check.py"
ALL = ("OPTION", "CUTOFF", "ORDER", "QUIET")


@pytest.fixture
def dbs(tmp_path):
    """pre_7a.db: D3 state (layer off, an active home with pending cases).
    live.db: the same after a restart in shadow - cut-off, then a quiet
    startup sweep."""
    storage = Storage(tmp_path / "pre_7a.db", precedent_mode="off")
    sweep(storage)
    storage.connection.close()
    shutil.copy(tmp_path / "pre_7a.db", tmp_path / "live.db")
    _options(tmp_path, "shadow")
    return tmp_path


def _options(tmp: Path, precedent: str, observation: str = "active") -> None:
    (tmp / "options.json").write_text(
        f'{{"observation_mode": "{observation}", "precedent_mode": "{precedent}"}}',
        encoding="utf-8")


def _enable(tmp: Path) -> Storage:
    storage = start_layer(tmp / "live.db")
    sweep(storage)
    return storage


def _run(tmp: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--live-db", str(tmp / "live.db"),
         "--pre-db", str(tmp / "pre_7a.db"), "--options", str(tmp / "options.json")],
        capture_output=True, text=True)


def _sections(stdout: str) -> dict:
    return {line.split()[0]: line.split()[1] for line in stdout.splitlines()
            if len(line.split()) == 2 and line.split()[0] in ALL}


def test_clean_enable_passes_and_live_is_untouched(dbs):
    _enable(dbs).connection.close()
    before = hashlib.sha256((dbs / "live.db").read_bytes()).hexdigest()
    result = _run(dbs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _sections(result.stdout) == dict.fromkeys(ALL, "PASS")
    for expected in ("precedent_audit rows: 1; layer_started: 1", "source=None",
                     "active startup", "AFTER the cut-off; unchanged/classified/no_match/error = 4/0/0/0",
                     "cut-off precedes the first startup sweep", "annotation_failed: 0 (must be 0)",
                     "classification_observations: +0 since the snapshot", "RESULT: PASS"):
        assert expected in result.stdout, expected
    assert hashlib.sha256((dbs / "live.db").read_bytes()).hexdigest() == before


def test_before_the_restart_everything_but_the_option_fails(dbs):
    result = _run(dbs)
    assert result.returncode == 1
    assert _sections(result.stdout) == {"OPTION": "PASS", "CUTOFF": "FAIL", "ORDER": "FAIL",
                                        "QUIET": "PASS"}
    assert "0 layer_started rows, expected exactly 1" in result.stdout


def test_event_sweep_of_the_old_process_before_the_cut_off_is_allowed(dbs):
    storage = Storage(dbs / "live.db", precedent_mode="off")
    sweep(storage, source="entity_registry_updated")
    storage.connection.close()
    _enable(dbs).connection.close()
    result = _run(dbs)
    assert result.returncode == 0, result.stdout
    assert "entity_registry_updated" in result.stdout and "BEFORE the cut-off" in result.stdout


def test_startup_sweep_before_the_cut_off_fails_order(dbs):
    """Core swept first and wrote the cut-off afterwards."""
    storage = Storage(dbs / "live.db", precedent_mode="shadow")
    sweep(storage)
    storage.ensure_precedent_layer_started()
    storage.connection.close()
    result = _run(dbs)
    assert _sections(result.stdout)["ORDER"] == "FAIL"
    assert "started before the cut-off" in result.stdout
    assert "no sweep after the cut-off yet" in result.stdout


def test_first_sweep_after_the_cut_off_must_be_the_startup_sweep(dbs):
    storage = start_layer(dbs / "live.db")
    sweep(storage, source="daily")
    storage.connection.close()
    result = _run(dbs)
    assert _sections(result.stdout)["ORDER"] == "FAIL"
    assert "first sweep after the cut-off is daily, not startup" in result.stdout


@pytest.mark.parametrize("precedent, observation, expected", [
    ("off", "active", "precedent_mode is 'off', expected 'shadow'"),
    ("shadow", "shadow", "observation_mode is 'shadow', expected 'active'"),
])
def test_wrong_option_fails(dbs, precedent, observation, expected):
    _enable(dbs).connection.close()
    _options(dbs, precedent, observation)
    result = _run(dbs)
    assert _sections(result.stdout)["OPTION"] == "FAIL" and expected in result.stdout


def test_a_decision_or_new_evidence_after_the_snapshot_fails_quiet(dbs):
    storage = _enable(dbs)
    case = storage.get_pending_review_cases()[0]
    storage.resolve_review_case(case["case_id"], case["observation_id"], "approved")
    pending(storage, "dev_new")
    storage.connection.close()
    result = _run(dbs)
    assert _sections(result.stdout)["QUIET"] == "FAIL"
    assert "review cases by status differ from the snapshot" in result.stdout
    assert "1 new classification_observations row(s) since the snapshot" in result.stdout


def test_annotation_failed_row_fails_quiet(dbs):
    _enable(dbs).connection.close()
    conn = sqlite3.connect(dbs / "live.db")
    conn.execute("INSERT INTO precedent_audit (id, event, source, policy_version, created_at) "
                 "VALUES ('pau_x', 'annotation_failed', 'observation', '7a.1', 't')")
    conn.commit()
    conn.close()
    result = _run(dbs)
    assert _sections(result.stdout)["QUIET"] == "FAIL"
    assert "1 annotation_failed row(s)" in result.stdout


def test_missing_snapshot_is_a_clear_message(dbs):
    (dbs / "pre_7a.db").unlink()
    result = _run(dbs)
    assert result.returncode == 1 and "Traceback" not in result.stdout + result.stderr
    assert "RESULT: FAIL - snapshot" in result.stdout
