"""Tests for scripts/d1_verify.py (D1 database-level immutability check)."""

import hashlib
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from src.storage import OBSERVATION_IDENTITY_TRIGGER, RAW_HYPOTHESIS_COLUMNS, Storage
from tests.test_m0_precheck import DEVICE_NAME, _build_db
from tests.test_storage import _obs

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "d1_verify.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("d1_verify", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def d1_era_schema(monkeypatch):
    """d1_verify.py checks the database as D1 left it. Reconstruct that
    schema by switching off the STEP 7P additions (tables, input_id,
    5 triggers); tests of the post-7P schema live in test_d2_verify.py."""
    monkeypatch.setattr(Storage, "init_step7p_schema", lambda self, cursor: None)
    monkeypatch.setattr(Storage, "init_step7a_schema", lambda self, cursor: None)


@pytest.fixture
def dbs():
    """pre_d1.db = pre-M1 database (no triggers); live.db = same after startup."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        s = _build_db(tmp / "pre.db")
        for (name,) in s.connect().execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
            s.connect().execute(f"DROP TRIGGER {name}")
        s.connect().commit()
        s.connection.close()
        shutil.copy(tmp / "pre.db", tmp / "live.db")
        Storage(tmp / "live.db").connection.close()  # D1 startup adds triggers
        yield tmp


def _run(tmp: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--src-root", str(REPO),
         "--live-db", str(tmp / "live.db"), "--pre-db", str(tmp / "pre.db"),
         "--work-db", str(tmp / "work.db")],
        capture_output=True, text=True,
    )


def _live(tmp: Path) -> Storage:
    return Storage(tmp / "live.db")


def test_column_list_matches_deployed_constant():
    module = _load_script()
    assert module.RAW_COLUMNS == RAW_HYPOTHESIS_COLUMNS
    assert module.IDENTITY_TRIGGER == OBSERVATION_IDENTITY_TRIGGER


def test_pass_and_live_db_untouched(dbs):
    before = hashlib.sha256((dbs / "live.db").read_bytes()).hexdigest()
    result = _run(dbs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "raw-hypothesis triggers: 12/12" in result.stdout
    assert "observation-identity trigger: present" in result.stdout
    assert "unexpected triggers: none" in result.stdout
    assert "refused with exact message: 13/13" in result.stdout
    assert "RESULT: PASS" in result.stdout
    assert DEVICE_NAME not in result.stdout
    assert hashlib.sha256((dbs / "live.db").read_bytes()).hexdigest() == before


def test_new_observation_after_snapshot_is_allowed(dbs):
    s = _live(dbs)
    new = _obs(s, "g9", "dev1", "env", "environmental")
    s.upsert_review_case("dev1", "env", "environmental", new)  # pointer advances
    s.connection.close()
    result = _run(dbs)
    assert result.returncode == 0, result.stdout
    assert "1 pointer(s) advanced" in result.stdout


def test_changed_snapshot_row_fails(dbs):
    s = _live(dbs)
    s.connect().execute("UPDATE classification_observations SET review_reason = 'x'")
    s.connect().commit()
    s.connection.close()
    result = _run(dbs)
    assert result.returncode == 1
    assert "classification_observations: row obs_" in result.stdout


@pytest.mark.parametrize("sql, expected", [
    ("DROP TRIGGER trg_obs_immutable_device_id", "missing trigger trg_obs_immutable_device_id"),
    ("DROP TRIGGER trg_obs_immutable_id", "missing trigger trg_obs_immutable_id"),
    ("CREATE TRIGGER trg_extra AFTER INSERT ON assets BEGIN SELECT 1; END",
     "unexpected trigger trg_extra"),
])
def test_trigger_set_must_be_exact(dbs, sql, expected):
    s = _live(dbs)
    s.connection.close()
    import sqlite3
    conn = sqlite3.connect(dbs / "live.db")
    conn.execute(sql)
    conn.commit()
    conn.close()
    result = _run(dbs)
    assert result.returncode == 1
    assert expected in result.stdout


def test_d1_verify_rejects_a_step7p_database(dbs, monkeypatch):
    """After D2 the D1 verifier is expected to FAIL (unexpected 7P triggers):
    it is an evidence tool for the D1 state, superseded by d2_verify.py."""
    monkeypatch.undo()
    Storage(dbs / "live.db").connection.close()  # STEP 7P startup
    result = _run(dbs)
    assert result.returncode == 1
    assert "unexpected trigger trg_obs_immutable_input_id" in result.stdout
