"""Tests for scripts/m0_precheck.py (production database baseline check)."""

import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

import pytest

from src.storage import Storage
from tests.test_storage import _obs

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "m0_precheck.py"
DEVICE_NAME = "Secret Living Room Sensor"


def _run(db_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(db_path)],
        capture_output=True, text=True,
    )


def _build_db(path: Path) -> Storage:
    storage = Storage(path)
    o1 = _obs(storage, "g1", "dev1", "env", "environmental", name=DEVICE_NAME)
    c1 = storage.upsert_review_case("dev1", "env", "environmental", o1)
    storage.resolve_review_case(
        c1, expected_observation_id=o1,
        decision="corrected", corrected_category="occupancy",
    )
    o2 = _obs(storage, "g2", "dev2", "motion", "motion", name=DEVICE_NAME)
    storage.upsert_review_case("dev2", "motion", "motion", o2)
    storage.pending_obs = o2
    return storage


@pytest.fixture
def db_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def test_clean_database_passes_and_is_not_modified(db_dir):
    storage = _build_db(db_dir / "clean.db")
    storage.connection.close()
    before = (db_dir / "clean.db").read_bytes()

    result = _run(db_dir / "clean.db")

    assert result.returncode == 0, result.stdout
    assert "RESULT: CLEAN" in result.stdout
    assert "NOT a proof that historical overwrites never occurred" in result.stdout
    assert "[INFO] review_cases pending: 1" in result.stdout
    assert "[INFO] review_cases resolved: 1" in result.stdout
    assert "[INFO] observations carrying a human decision: 1" in result.stdout
    # Storage now creates the 13 M1 triggers (12 raw + identity); listed as INFO.
    assert "[INFO] SQLite triggers: 13 (trg_obs_immutable_classifier_name" in result.stdout
    assert "script SHA256:" in result.stdout and "database size:" in result.stdout
    assert DEVICE_NAME not in result.stdout
    assert (db_dir / "clean.db").read_bytes() == before


def test_inconsistencies_fail_and_report_ids_only(db_dir):
    storage = _build_db(db_dir / "broken.db")
    # legacy path labels the evidence row of a still-pending case
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        storage.approve_observation(storage.pending_obs)
    # resolved case whose decision no longer matches its labelled observation
    storage.connect().execute(
        "UPDATE review_cases SET decision = 'approved', corrected_category = NULL "
        "WHERE status = 'resolved'"
    )
    storage.connection.commit()
    storage.connection.close()

    result = _run(db_dir / "broken.db")

    assert result.returncode == 1
    assert "RESULT: 2 CHECK(S) FAILED" in result.stdout
    assert result.stdout.count("[FAIL]") == 2
    assert "case_" in result.stdout  # offending review case ids are listed
    assert DEVICE_NAME not in result.stdout


def test_missing_file_is_usage_error(db_dir):
    assert _run(db_dir / "nope.db").returncode == 2
