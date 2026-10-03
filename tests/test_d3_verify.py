"""scripts/d3_verify.py - cumulative D0 + D1 + D2 + D3, the canonical
resolver, the precedent layer and the rehearsal forecast. Read-only on live."""

import copy
import hashlib
import importlib.metadata
import importlib.util
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.storage import Storage
from tests.ha_payloads import full_home
from tests.precedent_helpers import rows, start_layer, sweep
from tests.test_7p_equivalence import deployed_tree  # noqa: F401  (fixture)
from tests.test_m0_precheck import DEVICE_NAME, _build_db

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "d3_verify.py"
spec = importlib.util.spec_from_file_location("d3_verify", SCRIPT)
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)

ALL = ("D0", "D1", "D2", "D3", "PINS", "DATA", "RESOLVER", "LAYER", "REHEARSAL")
INHERITED = ("D0", "D1", "D2", "PINS", "DATA")
MOTION = "motion_sensor"


def _step7p_era(fn):
    original = Storage.init_step7a_schema
    Storage.init_step7a_schema = lambda self, cursor: None
    try:
        return fn()
    finally:
        Storage.init_step7a_schema = original


def _production_like(path: Path) -> None:
    """STEP 7P active: two resolved cases (one of them on a hypothesis the
    sweep observes on another device too) and four pending ones."""
    storage = _build_db(path)                       # dev1 corrected, dev2 pending
    sweep(storage)                                  # dev_motion, dev_multi x2, dev_socket
    (case,) = rows(storage, "review_cases", where=(
        f"WHERE device_id = 'dev_motion' AND classifier_name = '{MOTION}'"))
    storage.resolve_review_case(case["id"], case["last_observation_id"], "approved")
    storage.connection.close()


@pytest.fixture
def dbs(tmp_path):
    """pre_d3.db: the STEP 7P database. live.db: the same after the D3
    deployment - schema migrated at startup, layer off, one quiet sweep."""
    _step7p_era(lambda: _production_like(tmp_path / "pre_d3.db"))
    shutil.copy(tmp_path / "pre_d3.db", tmp_path / "live.db")
    storage = Storage(tmp_path / "live.db", precedent_mode="off")
    sweep(storage, source="daily")
    storage.connection.close()
    (tmp_path / "work").mkdir()
    (tmp_path / "requirements.txt").write_text(
        f"pytest=={importlib.metadata.version('pytest')}\n", encoding="utf-8")
    _option(tmp_path, "off")
    return tmp_path


def _option(tmp: Path, mode: str) -> None:
    (tmp / "options.json").write_text(
        f'{{"observation_mode": "active", "precedent_mode": "{mode}"}}', encoding="utf-8")


def _run(tmp: Path, src_root: Path = REPO, live: str = "live.db") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--src-root", str(src_root),
         "--live-db", str(tmp / live), "--pre-db", str(tmp / "pre_d3.db"),
         "--work-dir", str(tmp / "work"), "--requirements", str(tmp / "requirements.txt"),
         "--options", str(tmp / "options.json")],
        capture_output=True, text=True)


def _sections(stdout: str) -> dict:
    return {line.split()[0]: line.split()[1] for line in stdout.splitlines()
            if len(line.split()) == 2 and line.split()[0] in ALL}


def _sql(tmp: Path, *statements) -> None:
    conn = sqlite3.connect(tmp / "live.db")
    for sql in statements:
        conn.execute(sql)
    conn.commit()
    conn.close()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _renamed():
    devices, entities, states = copy.deepcopy(full_home())
    devices[1]["name"] = "Renamed"
    return devices, entities, states


# --- the D3 state: deployed, layer off, never started -----------------------------

def test_d3_state_passes_and_live_is_untouched(dbs):
    before = _sha(dbs / "live.db")
    result = _run(dbs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _sections(result.stdout) == dict.fromkeys(ALL, "PASS")
    for expected in (
            "STEP 7P triggers present: 5/5",
            "STEP 7a triggers present: 6/6",
            "triggers in total: 24 (expected 24); unexpected: none",
            "append-only refusals with exact message: 6/6",
            "CHECK/UNIQUE constraints enforced: 9/9",
            "statements writing a decision: [('storage.py', 'label'), ('storage.py', 'resolve')]",
            "legacy calls refused as not_reviewable: 3/3; decision fields identical: yes",
            "legacy call on current evidence resolves through the resolver: yes",
            "option precedent_mode: off",
            "logical keys with more than one labelled observation: 0 (must be 0)",
            "layer_started rows: 0 (layer never started)",
            "annotations: 0; annotation_failed: observation 0, bootstrap_pending 0",
            "FORECAST - not a gate invariant. Live database untouched.",
            "pending cases: 4; order: as the `review` CLI lists them",
            f"{MOTION}/{MOTION}: class_pattern suggestion evidence 1/1 insufficient n=1",
            "forecast for this order: annotations written for 1 case(s), "
            "without evidence 3, annotation_failed 0",
            "resolved cases re-decided on a copy: 2",
            "RESULT: PASS"):
        assert expected in result.stdout, expected
    assert DEVICE_NAME not in result.stdout and "Desk lamp" not in result.stdout
    assert _sha(dbs / "live.db") == before
    conn = sqlite3.connect(dbs / "live.db")
    assert conn.execute("SELECT COUNT(*) FROM review_cases WHERE status = 'pending'").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM precedent_audit").fetchone()[0] == 0
    conn.close()


def test_negative_control_old_code_on_the_old_database(dbs, deployed_tree):  # noqa: F811
    """The deployed STEP 7P tree on the STEP 7P database: everything
    inherited passes, every new section fails."""
    result = _run(dbs, src_root=deployed_tree, live="pre_d3.db")
    assert result.returncode == 1
    assert _sections(result.stdout) == {**dict.fromkeys(INHERITED, "PASS"),
                                        **dict.fromkeys(("D3", "RESOLVER", "LAYER", "REHEARSAL"),
                                                        "FAIL")}
    assert "STEP 7a triggers present: 0/6" in result.stdout
    assert "a legacy method did not refuse a non-current observation" in result.stdout
    assert "precedent tables absent" in result.stdout


def test_missing_snapshot_is_a_clear_message_not_a_traceback(dbs):
    (dbs / "pre_d3.db").unlink()
    result = _run(dbs)
    assert result.returncode == 1 and "Traceback" not in result.stdout + result.stderr
    assert "RESULT: FAIL - snapshot" in result.stdout and "take the backup first" in result.stdout


# --- the enable gate and afterwards ---------------------------------------------------

def test_layer_started_and_nothing_else(dbs):
    start_layer(dbs / "live.db").connection.close()
    _option(dbs, "shadow")
    result = _run(dbs)
    assert result.returncode == 0, result.stdout
    assert "option precedent_mode: shadow" in result.stdout
    assert "layer_started rows: 1 (cut-off " in result.stdout
    assert "annotations fully consistent: 0/0" in result.stdout
    assert "pending cases: 4" in result.stdout


def _reviewed(tmp: Path) -> None:
    """Layer in shadow: the four pending cases resolved (bootstrap cohort),
    then a registry change observed (normal cohort + device precedent)."""
    storage = start_layer(tmp / "live.db")
    for case in storage.get_pending_review_cases():
        storage.resolve_review_case(case["case_id"], case["observation_id"], "rejected")
    sweep(storage, _renamed(), "device_registry_updated")
    storage.connection.close()
    _option(tmp, "shadow")


def test_after_the_review_every_annotation_is_consistent(dbs):
    _reviewed(dbs)
    before = _sha(dbs / "live.db")
    result = _run(dbs)
    assert result.returncode == 0, result.stdout
    assert _sections(result.stdout) == dict.fromkeys(ALL, "PASS")
    for expected in (
            "annotations: 4; annotation_failed: observation 0, bootstrap_pending 0",
            "('class_pattern', 'bootstrap_pending'): 1",
            "('class_pattern', 'observation'): 1",
            "('device_precedent', 'observation'): 2",
            "annotations fully consistent: 4/4",
            "classification_observations: 6 snapshot rows checked, +2 new, "
            "0 pointer(s) advanced, 4 decided since the snapshot"):
        assert expected in result.stdout, expected
    assert result.stdout.count("4 decided since the snapshot") == 2   # and their cases
    assert _sha(dbs / "live.db") == before


def test_layer_back_to_off_keeps_its_history_and_passes(dbs):
    _reviewed(dbs)
    _option(dbs, "off")
    result = _run(dbs)
    assert result.returncode == 0, result.stdout
    assert "option precedent_mode: off" in result.stdout
    assert "layer_started rows: 1" in result.stdout


# --- tampering ------------------------------------------------------------------------

@pytest.mark.parametrize("statements, section, expected", [
    (["DROP TRIGGER trg_precedent_audit_no_delete"], "D3",
     "missing trigger trg_precedent_audit_no_delete"),
    (["CREATE TRIGGER trg_extra AFTER INSERT ON assets BEGIN SELECT 1; END"], "D3",
     "unexpected trigger trg_extra"),
    (["DROP INDEX idx_precedent_layer_started"], "D3", "missing index idx_precedent_layer_started"),
    (["DROP TRIGGER trg_classification_sweeps_no_update"], "D2",
     "missing trigger trg_classification_sweeps_no_update"),
    (["INSERT INTO precedent_audit (id, event, source, policy_version, created_at) "
      "VALUES ('pau_1', 'annotation_failed', 'observation', '7a.1', 't')"], "LAYER",
     "precedent rows exist although the layer never started"),
    (["INSERT INTO classification_observations (id, observation_group_id, device_id, "
      "classifier_name, hypothesis_category, hypothesis_confidence, created_at, "
      "device_source_adapter, device_entity_count, "
      "review_status, human_decision, reviewed_at) VALUES ('obs_dup', 'g', 'dev1', 'env', "
      "'environmental', 0.5, '2026-01-01T00:00:00Z', 'ha', 1, 'reviewed', 'approved', "
      "'2026-01-02T00:00:00Z')"], "LAYER",
     "1 logical key(s) with more than one labelled observation"),
    (["UPDATE review_cases SET decision = 'approved' WHERE device_id = 'dev1'"], "DATA",
     "decision/identity changed"),
    (["UPDATE review_cases SET status = 'pending', decision = NULL, decided_at = NULL "
      "WHERE device_id = 'dev1'"], "DATA", "decision/identity changed"),
    (["UPDATE classification_observations SET human_decision = 'approved' "
      "WHERE device_id = 'dev1'"], "DATA", "decision changed"),
    (["UPDATE classification_observations SET reviewed_by = 'x' WHERE device_id = 'dev2'"],
     "DATA", "decision changed"),
    (["UPDATE review_cases SET device_id = 'dev_x' WHERE device_id = 'dev2'"], "DATA",
     "identity changed"),
    (["DROP TRIGGER trg_classification_sweeps_no_delete",
      "DELETE FROM classification_sweeps WHERE rowid = 1"], "DATA",
     "classification_sweeps: row"),
])
def test_tampered_database_fails_its_section(dbs, statements, section, expected):
    _sql(dbs, *statements)
    result = _run(dbs)
    assert result.returncode == 1
    assert _sections(result.stdout)[section] == "FAIL"
    assert expected in result.stdout


def test_recreated_trigger_with_a_wrong_message_fails_d3(dbs):
    _sql(dbs, "DROP TRIGGER trg_precedent_audit_no_update",
         "CREATE TRIGGER trg_precedent_audit_no_update BEFORE UPDATE ON precedent_audit "
         "BEGIN SELECT RAISE(ABORT, 'nope'); END")
    result = _run(dbs)
    assert _sections(result.stdout)["D3"] == "FAIL"
    assert "precedent_audit updated: unexpected message nope" in result.stdout


def test_option_shadow_without_a_cut_off_fails_layer(dbs):
    _option(dbs, "shadow")
    result = _run(dbs)
    assert _sections(result.stdout)["LAYER"] == "FAIL"
    assert "option is shadow but the layer never started" in result.stdout


def _tree(tmp: Path, name: str, old: str, new: str) -> Path:
    root = tmp / "tree"
    shutil.copytree(REPO / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    path = root / "src" / name
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return root


def test_second_decision_path_in_the_code_fails_resolver(dbs):
    tree = _tree(dbs, "main.py", "def show_precedent_report():", (
        "def relabel(conn, obs):\n"
        "    conn.execute(\"UPDATE classification_observations SET human_decision = ? "
        "WHERE id = ?\", ('approved', obs))\n\n\ndef show_precedent_report():"))
    result = _run(dbs, src_root=tree)
    assert _sections(result.stdout)["RESOLVER"] == "FAIL"
    assert "decision writes are not exactly one label and one resolve" in result.stdout


def test_legacy_bypass_in_the_code_fails_resolver(dbs):
    tree = _tree(dbs, "storage.py", '            return DecisionResult("not_reviewable")',
                 '            return DecisionResult("resolved")')
    result = _run(dbs, src_root=tree)
    assert _sections(result.stdout)["RESOLVER"] == "FAIL"
    assert "a legacy method did not refuse a non-current observation" in result.stdout


def test_failing_annotator_is_reported_by_the_rehearsal_but_is_not_a_gate_failure(dbs):
    """The counts are a forecast: an annotation that fails while every
    decision is committed is reported, and the section still passes."""
    tree = _tree(dbs, "precedent_store.py", '    annotation_id = f"pan_{uuid4().hex[:12]}"',
                 '    raise RuntimeError("broken store")')
    result = _run(dbs, src_root=tree)
    assert _sections(result.stdout)["REHEARSAL"] == "PASS"
    assert f"{MOTION}/{MOTION}: annotation FAILED (RuntimeError)" in result.stdout
    assert "annotations written for 0 case(s), without evidence 3, annotation_failed 1" \
        in result.stdout


def test_resolver_that_raises_fails_the_rehearsal(dbs):
    tree = _tree(dbs, "storage.py", "            if cursor.rowcount != 1:\n"
                 "                raise RuntimeError(\n"
                 "                    f\"Expected exactly one review case",
                 "            if True:\n                raise RuntimeError(\n"
                 "                    f\"Expected exactly one review case")
    result = _run(dbs, src_root=tree)
    assert _sections(result.stdout)["REHEARSAL"] == "FAIL"
    assert "exception escaped the resolver" in result.stdout
    assert "4 case(s) still pending after the rehearsal" in result.stdout


# --- LAYER: one annotation against its evidence ---------------------------------------

CUT_OFF = "2026-10-05T00:00:00Z"


def _layer_case():
    evidence = [{"annotation_id": "pan_1", "review_case_id": "case_9",
                 "labelled_observation_id": "obs_9", "device_id": "dev9",
                 "human_decision": "approved", "corrected_category": None,
                 "decided_at": "2026-10-01T10:00:00Z"}]
    annotation = {"id": "pan_1", "observation_id": "obs_1", "memory_type": "class_pattern",
                  "annotation_trigger": "bootstrap_pending", "device_id": "dev1",
                  "sample_size": 1, "policy_version": "7a.1",
                  "evidence_digest": verify._digest(evidence, "7a.1"),
                  "created_at": "2026-10-06T10:00:00Z"}
    observations = {
        "obs_1": {"created_at": "2026-10-02T10:00:00Z", "reviewed_at": "2026-10-06T10:00:00.5Z",
                  "human_decision": "rejected", "corrected_category": None},
        "obs_9": {"created_at": "2026-09-30T10:00:00Z", "reviewed_at": "2026-10-01T10:00:00Z",
                  "human_decision": "approved", "corrected_category": None}}
    return annotation, evidence, observations


def test_consistent_annotation_passes_and_digest_equals_the_codes():
    from src.learning.precedent import Decision, evidence_digest
    annotation, evidence, observations = _layer_case()
    assert verify._check_annotation(annotation, evidence, observations, CUT_OFF) == []
    assert annotation["evidence_digest"] == evidence_digest(
        [Decision("obs_9", "dev9", "approved", None, "2026-10-01T10:00:00Z", "case_9")])


@pytest.mark.parametrize("where, change, expected", [
    ("annotation", {"evidence_digest": "0" * 64}, "evidence digest does not match"),
    ("annotation", {"sample_size": 2}, "1 evidence rows for sample_size 2"),
    ("annotation", {"created_at": "2026-10-04T10:00:00Z"}, "written before the cut-off"),
    ("annotation", {"created_at": "2026-10-06T10:00:01Z"},
     "not written before the decision on its observation"),
    ("annotation", {"annotation_trigger": "observation"},
     "trigger observation on an observation older than the cut-off"),
    ("annotation", {"observation_id": "obs_gone"}, "annotated observation does not exist"),
    ("annotation", {"memory_type": "device_precedent"}, "device_precedent evidence from the wrong device"),
    ("annotation", {"device_id": "dev9"}, "class_pattern evidence from the wrong device"),
    ("evidence", {"human_decision": "rejected"}, "evidence differs from the labelled observation"),
    ("evidence", {"decided_at": "2026-10-07T10:00:00Z"}, "evidence decided after the annotation"),
    ("observation", {"created_at": "2026-10-05T10:00:00Z"},
     "trigger bootstrap_pending on an observation newer than the cut-off"),
])
def test_inconsistent_annotation_is_reported(where, change, expected):
    annotation, evidence, observations = _layer_case()
    {"annotation": annotation, "evidence": evidence[0],
     "observation": observations["obs_1"]}[where].update(change)
    failures = verify._check_annotation(annotation, evidence, observations, CUT_OFF)
    assert any(expected in f for f in failures), failures
