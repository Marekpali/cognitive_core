"""T13: scripts/d2_verify.py (STEP 7P schema + shadow start, read-only)."""

import asyncio
import hashlib
import importlib.util
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from src.coverage_store import STEP7P_TRIGGERS
from src.storage import Storage
from src.sweep import ObservationSweep
from tests.ha_payloads import full_home
from tests.test_m0_precheck import DEVICE_NAME, _build_db
from tests.test_storage import _obs
from tests.test_sweep import _classifiers

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "d2_verify.py"


def _d1_era(fn):
    original = Storage.init_step7p_schema
    Storage.init_step7p_schema = lambda self, cursor: None
    try:
        return fn()
    finally:
        Storage.init_step7p_schema = original


@pytest.fixture
def dbs():
    """pre.db = D1-era database with history; live.db = same after the
    STEP 7P startup and one shadow sweep."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        _d1_era(lambda: _build_db(tmp / "pre.db").connection.close())
        shutil.copy(tmp / "pre.db", tmp / "live.db")
        live = Storage(tmp / "live.db")
        asyncio.run(ObservationSweep(live, _classifiers(), "shadow").run(
            "startup", *full_home()))
        live.connection.close()
        yield tmp


def _run(tmp: Path, live="live.db") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--src-root", str(REPO),
         "--live-db", str(tmp / live), "--pre-db", str(tmp / "pre.db"),
         "--work-db", str(tmp / "work.db")],
        capture_output=True, text=True,
    )


def _sql(tmp: Path, sql: str, params=()):
    conn = sqlite3.connect(tmp / "live.db")
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def test_trigger_names_match_the_code():
    spec = importlib.util.spec_from_file_location("d2_verify", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.STEP7P_TRIGGERS == set(STEP7P_TRIGGERS)


def test_pass_and_live_db_untouched(dbs):
    before = hashlib.sha256((dbs / "live.db").read_bytes()).hexdigest()
    result = _run(dbs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "D1 triggers: 13/13" in result.stdout
    assert "STEP 7P triggers: 5/5" in result.stdout
    assert "unexpected triggers: none" in result.stdout
    assert "pre-D2 observations with input_id: 0" in result.stdout
    assert "classification_inputs rows: 0" in result.stdout
    assert "refused with exact message: 18/18" in result.stdout
    assert "RESULT: PASS" in result.stdout
    assert DEVICE_NAME not in result.stdout and "Desk lamp" not in result.stdout
    assert hashlib.sha256((dbs / "live.db").read_bytes()).hexdigest() == before


def test_negative_control_pre_d2_database_fails(dbs):
    result = _run(dbs, live="pre.db")
    assert result.returncode == 1
    assert "missing trigger trg_obs_immutable_input_id" in result.stdout


def test_active_sweep_or_written_input_fails_shadow_check(dbs):
    live = Storage(dbs / "live.db")
    asyncio.run(ObservationSweep(live, _classifiers(), "active").run(
        "startup", *full_home()))
    live.connection.close()
    result = _run(dbs)
    assert result.returncode == 1
    assert "shadow: 4 classification_inputs row(s) written" in result.stdout
    assert "ran in mode active" in result.stdout


def test_new_observation_fails_shadow_check(dbs):
    live = Storage(dbs / "live.db")
    _obs(live, "g9", "dev9", "env", "environmental")
    live.connection.close()
    result = _run(dbs)
    assert result.returncode == 1
    assert "shadow: 1 observation(s) written" in result.stdout


def test_changed_snapshot_row_fails(dbs):
    _sql(dbs, "UPDATE classification_observations SET review_reason = 'x'")
    result = _run(dbs)
    assert result.returncode == 1
    assert "classification_observations: row obs_" in result.stdout


@pytest.mark.parametrize("sql, expected", [
    ("DROP TRIGGER trg_classification_sweeps_no_delete",
     "missing trigger trg_classification_sweeps_no_delete"),
    ("DROP TRIGGER trg_obs_immutable_device_id", "missing trigger trg_obs_immutable_device_id"),
    ("CREATE TRIGGER trg_extra AFTER INSERT ON assets BEGIN SELECT 1; END",
     "unexpected trigger trg_extra"),
])
def test_trigger_set_must_be_exact(dbs, sql, expected):
    _sql(dbs, sql)
    result = _run(dbs)
    assert result.returncode == 1
    assert expected in result.stdout


def test_backfilled_input_id_on_pre_d2_row_fails(dbs):
    _sql(dbs, "DROP TRIGGER trg_obs_immutable_input_id")
    _sql(dbs, "UPDATE classification_observations SET input_id = 'inp_x'")
    result = _run(dbs)
    assert result.returncode == 1
