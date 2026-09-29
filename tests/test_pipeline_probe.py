"""Tests for scripts/pipeline_probe.py (read-only before/after counts)."""

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.test_m0_precheck import DEVICE_NAME, _build_db

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pipeline_probe.py"


def test_probe_reports_counts_read_only_without_device_data():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "live.db"
        storage = _build_db(db)
        newest = storage.pending_obs
        storage.connection.close()
        before = hashlib.sha256(db.read_bytes()).hexdigest()

        result = subprocess.run([sys.executable, str(SCRIPT), str(db)],
                                capture_output=True, text=True)

        assert result.returncode == 0, result.stderr
        assert "classification_observations: 2" in result.stdout
        assert "review_cases pending: 1" in result.stdout
        assert "review_cases resolved: 1" in result.stdout
        assert f"newest observation: {newest} @ " in result.stdout
        assert DEVICE_NAME not in result.stdout
        assert hashlib.sha256(db.read_bytes()).hexdigest() == before
