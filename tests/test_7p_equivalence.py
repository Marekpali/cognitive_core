"""STEP 7a gate: with precedent_mode off the refactored write path behaves
exactly like the deployed STEP 7P code (8570f77), and switching the layer to
shadow changes nothing STEP 7P writes.

The scenario (tests/equivalence_scenario.py) runs in a subprocess against
each source tree; random ids are mapped to ordinals and timestamps are
blanked before comparison.
"""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import io
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCENARIO = REPO / "tests" / "equivalence_scenario.py"
DEPLOYED = "8570f77"


def _run(src_root: Path, db: Path, **env) -> dict:
    environment = {k: v for k, v in os.environ.items()
                   if k not in ("PRECEDENT_MODE", "OBSERVATION_MODE", "START_LAYER")}
    environment.update(env, PYTHONPATH=str(src_root))
    result = subprocess.run([sys.executable, str(SCENARIO), str(db)], cwd=src_root,
                            env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    # proves which tree ran: only the refactored code has the single transaction
    assert output["single_transaction"] is (src_root == REPO)
    return json.loads(output["result"])


@pytest.fixture(scope="module")
def deployed_tree(tmp_path_factory) -> Path:
    """src/ and the test payloads exactly as in 8570f77, from git objects."""
    if not (REPO / ".git").exists() or shutil.which("git") is None:
        pytest.skip("needs the git history (8570f77)")
    archive = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "archive", DEPLOYED, "src",
         "tests/__init__.py", "tests/ha_payloads.py"],
        cwd=REPO, capture_output=True)
    assert archive.returncode == 0, archive.stderr.decode()
    root = tmp_path_factory.mktemp("deployed_8570f77")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(root, filter="data")
    assert not (root / "src" / "precedent_store.py").exists()
    return root


@pytest.fixture(scope="module")
def current_off(tmp_path_factory) -> dict:
    return _run(REPO, tmp_path_factory.mktemp("off") / "core.db")


def test_scenario_exercises_the_interesting_paths(current_off):
    log, rows = current_off["log"], current_off["rows"]
    outcomes = json.dumps(log)
    for expected in ("classified", "no_match", "unchanged", "error", "skipped"):
        assert expected in outcomes
    assert [e for e in log if isinstance(e, str)] == [
        "resolved", "already_resolved", "not_found", "stale", "resolved"]
    assert len(rows["classification_inputs"]) > 4
    assert len(rows["classification_sweeps"]) == 9
    assert rows["assets"] == []


def test_precedent_off_equals_deployed_8570f77(deployed_tree, current_off, tmp_path):
    """Same inputs, observations, review cases, gate decisions and sweep
    results as the code running in production."""
    deployed = _run(deployed_tree, tmp_path / "core.db")
    assert current_off["log"] == deployed["log"]
    for table, rows in deployed["rows"].items():
        assert current_off["rows"][table] == rows, table
    assert current_off == deployed


def test_precedent_shadow_changes_nothing_step_7p_writes(current_off, tmp_path):
    shadow = _run(REPO, tmp_path / "core.db", PRECEDENT_MODE="shadow", START_LAYER="1")
    assert shadow == current_off
