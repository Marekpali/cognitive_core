"""Tests for scripts/d0_verify.py (D0 production immutability check)."""

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.test_m0_precheck import DEVICE_NAME, _build_db

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "d0_verify.py"


@pytest.fixture
def live_db():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "live.db"
        storage = _build_db(path)
        storage.connection.close()
        yield path


def _run(live_db: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--src-root", str(REPO),
         "--live-db", str(live_db),
         "--work-db", str(live_db.parent / "work.db")],
        capture_output=True, text=True,
    )


def test_current_code_passes_without_touching_live_db(live_db):
    before = hashlib.sha256(live_db.read_bytes()).hexdigest()

    result = _run(live_db)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout
    assert result.stdout.count("-> already_resolved") == 2  # case + legacy
    assert DEVICE_NAME not in result.stdout
    assert hashlib.sha256(live_db.read_bytes()).hexdigest() == before


def test_fails_when_nothing_to_verify():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "empty.db"
        from src.storage import Storage
        Storage(path).connect().close()

        result = _run(path)

    assert result.returncode == 1
    assert "no resolved cases found" in result.stdout
