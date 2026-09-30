"""T13: scripts/d2_verify.py - cumulative D0 + D1 + D2 check, read-only."""

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
    """pre.db = D1-era database with history (one resolved, one pending case);
    live.db = same after the STEP 7P startup and one shadow sweep."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        _d1_era(lambda: _build_db(tmp / "pre.db").connection.close())
        shutil.copy(tmp / "pre.db", tmp / "live.db")
        live = Storage(tmp / "live.db")
        asyncio.run(ObservationSweep(live, _classifiers(), "shadow").run(
            "startup", *full_home()))
        live.connection.close()
        (tmp / "work").mkdir()
        yield tmp


def _pins(tmp: Path, lines) -> Path:
    path = tmp / "requirements.txt"
    path.write_text("\n".join(["# comment", *lines]) + "\n", encoding="utf-8")
    return path


def _run(tmp: Path, live="live.db", src_root=REPO, requirements=None) -> subprocess.CompletedProcess:
    import importlib.metadata
    requirements = requirements or _pins(tmp, [
        f"pytest=={importlib.metadata.version('pytest')}"])
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--src-root", str(src_root),
         "--live-db", str(tmp / live), "--pre-db", str(tmp / "pre.db"),
         "--work-dir", str(tmp / "work"), "--requirements", str(requirements)],
        capture_output=True, text=True,
    )


def _sections(stdout: str) -> dict:
    return {line.split()[0]: line.split()[1] for line in stdout.splitlines()
            if line.split()[:1] and line.split()[0] in ("D0", "D1", "D2", "PINS", "DATA", "SHADOW")
            and len(line.split()) == 2}


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


def test_pass_all_sections_and_live_db_untouched(dbs):
    before = hashlib.sha256((dbs / "live.db").read_bytes()).hexdigest()
    result = _run(dbs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _sections(result.stdout) == dict.fromkeys(
        ("D0", "D1", "D2", "PINS", "DATA", "SHADOW"), "PASS")
    for expected in ("resolved cases re-decided on a copy: 1",
                     "D1 triggers present: 13/13",
                     "D1 protected changes refused with exact message: 13/13",
                     "STEP 7P triggers present: 5/5",
                     "D2 protected changes refused with exact message: 5/5",
                     "pre-D2 observations with input_id: 0",
                     "classification_inputs rows: 0 (must be 0)",
                     "RESULT: PASS"):
        assert expected in result.stdout
    assert DEVICE_NAME not in result.stdout and "Desk lamp" not in result.stdout
    assert hashlib.sha256((dbs / "live.db").read_bytes()).hexdigest() == before


def test_negative_control_fails_only_in_d2(dbs):
    """Old container, pre-D2 database: D0/D1 must still pass."""
    result = _run(dbs, live="pre.db")
    assert result.returncode == 1
    assert _sections(result.stdout) == {"D0": "PASS", "D1": "PASS", "D2": "FAIL",
                                        "PINS": "PASS", "DATA": "PASS", "SHADOW": "PASS"}
    assert "missing trigger trg_obs_immutable_input_id" in result.stdout
    assert "missing table classification_inputs" in result.stdout


def _deployed_without(tmp_path, *guards) -> Path:
    """A copy of src/ whose storage.py lacks the given D0 guard snippets."""
    fake_root = tmp_path / "fake"
    shutil.copytree(REPO / "src", fake_root / "src",
                    ignore=shutil.ignore_patterns("__pycache__"))
    storage_py = fake_root / "src" / "storage.py"
    text = storage_py.read_text(encoding="utf-8")
    for guard in guards:
        assert guard in text
        text = text.replace(guard, "")
    storage_py.write_text(text, encoding="utf-8")
    return fake_root


def test_d0_regression_in_deployed_code_is_detected(dbs, tmp_path):
    """Deployed code without both D0 guards overwrites a decision -> D0 FAIL."""
    fake = _deployed_without(tmp_path, " AND status = 'pending'",
                             " AND human_decision IS NULL")
    result = _run(dbs, src_root=fake)
    assert result.returncode == 1
    assert _sections(result.stdout)["D0"] == "FAIL"
    assert "resolve_review_case returned 'resolved'" in result.stdout
    assert "decision fields changed after overwrite attempts" in result.stdout


def test_crashing_check_is_reported_as_section_fail(dbs, tmp_path):
    """Only the case guard removed: the observation guard raises instead of
    returning 'already_resolved'. Reported as D0 FAIL; other sections run."""
    fake = _deployed_without(tmp_path, " AND status = 'pending'")
    result = _run(dbs, src_root=fake)
    sections = _sections(result.stdout)
    assert sections["D0"] == "FAIL" and sections["D1"] == "PASS"
    assert "check raised RuntimeError" in result.stdout


def test_missing_d1_trigger_fails_d1(dbs):
    _sql(dbs, "DROP TRIGGER trg_obs_immutable_device_id")
    result = _run(dbs)
    sections = _sections(result.stdout)
    assert result.returncode == 1 and sections["D1"] == "FAIL" and sections["D2"] == "PASS"
    assert "missing trigger trg_obs_immutable_device_id" in result.stdout
    assert "device_id was NOT refused" in result.stdout


@pytest.mark.parametrize("sql, expected", [
    ("DROP TRIGGER trg_classification_sweeps_no_delete",
     "missing trigger trg_classification_sweeps_no_delete"),
    ("CREATE TRIGGER trg_extra AFTER INSERT ON assets BEGIN SELECT 1; END",
     "unexpected trigger trg_extra"),
])
def test_d2_trigger_set_must_be_exact(dbs, sql, expected):
    _sql(dbs, sql)
    result = _run(dbs)
    assert result.returncode == 1 and _sections(result.stdout)["D2"] == "FAIL"
    assert expected in result.stdout


def test_active_sweep_fails_shadow(dbs):
    live = Storage(dbs / "live.db")
    asyncio.run(ObservationSweep(live, _classifiers(), "active").run(
        "startup", *full_home()))
    live.connection.close()
    result = _run(dbs)
    assert _sections(result.stdout)["SHADOW"] == "FAIL"
    assert "shadow: 4 classification_inputs row(s) written" in result.stdout
    assert "shadow: 4 new review_cases row(s)" in result.stdout
    assert "ran in mode active" in result.stdout


def test_new_observation_fails_shadow(dbs):
    live = Storage(dbs / "live.db")
    _obs(live, "g9", "dev9", "env", "environmental")
    live.connection.close()
    result = _run(dbs)
    assert _sections(result.stdout)["SHADOW"] == "FAIL"
    assert "shadow: 1 new classification_observations row(s)" in result.stdout


def test_new_asset_fails_shadow(dbs):
    _sql(dbs, "INSERT INTO assets (id, name, hypothesis_category, hypothesis_confidence, "
              "lifecycle_discovered_at, device_data, created_at, updated_at) "
              "VALUES ('a', 'n', 'c', 0.5, 't', '{}', 't', 't')")
    result = _run(dbs)
    assert "shadow: 1 new assets row(s)" in result.stdout


def test_changed_snapshot_row_fails_data(dbs):
    _sql(dbs, "UPDATE classification_observations SET review_reason = 'x'")
    result = _run(dbs)
    assert _sections(result.stdout)["DATA"] == "FAIL"
    assert "classification_observations: row obs_" in result.stdout


def test_backfilled_input_id_on_pre_d2_row_fails(dbs):
    _sql(dbs, "DROP TRIGGER trg_obs_immutable_input_id")
    _sql(dbs, "UPDATE classification_observations SET input_id = 'inp_x'")
    result = _run(dbs)
    assert result.returncode == 1
    assert "pre-D2 observation(s) gained an input_id" in result.stdout


def test_version_drift_fails_pins(dbs):
    result = _run(dbs, requirements=_pins(dbs, ["pytest==0.0.1", "not-installed-pkg==1.0"]))
    assert _sections(result.stdout)["PINS"] == "FAIL"
    assert "pytest: installed" in result.stdout and "pinned 0.0.1" in result.stdout
    assert "not-installed-pkg: installed absent" in result.stdout


def test_missing_requirements_file_fails_pins(dbs):
    result = _run(dbs, requirements=dbs / "nope.txt")
    assert _sections(result.stdout)["PINS"] == "FAIL"
