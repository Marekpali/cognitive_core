"""
D3 production verification (STEP 7a) - cumulative: D0 + D1 + D2 + D3 + data
preservation + the canonical resolver + the precedent layer + a rehearsal.
The D0, D1 and PINS sections are those of d2_verify.py, which must lie
next to this file.

Run INSIDE the container with the pre-deployment backup at /tmp/pre_d3.db:

    docker exec app_local_cognitive_core python3 /share/d3/d3_verify.py

  D0, D1, PINS  as in d2_verify.py.
  D2         the 5 STEP 7P triggers and their exact refusals.
  DATA       every pre_d3.db row still present. A human decision present in
             the snapshot is identical; an undecided observation/case may
             have been decided once (review fields only); a case pointer may
             only advance to evidence newer than the snapshot.
  D3         the 6 precedent triggers, 3 tables and the cut-off index exist;
             no trigger outside the 24 expected; on a copy UPDATE/DELETE are
             refused with the exact message and every CHECK/UNIQUE holds.
  RESOLVER   deployed code: exactly one statement labels an observation and
             one resolves a case, both inside resolve_review_case(); on a
             copy the legacy methods refuse every observation that is not
             the current evidence of a pending case ('not_reviewable') and
             delegate for one that is.
  LAYER      no logical key has more than one labelled observation (the
             annotator refuses such evidence). Consistent with the option
             and the history. Never started: no
             audit row, no annotation. Started: exactly one layer_started;
             every annotation has its evidence rows, a matching digest,
             was written before the decision on its observation and after
             all of its evidence; class-pattern evidence never contains the
             annotated device; bootstrap_pending only on observations older
             than the cut-off, 'observation' only on newer ones.
  REHEARSAL  FORECAST, NOT A GATE INVARIANT. On a copy, layer forced to
             shadow, every pending case is resolved in the order the
             `review` CLI lists them. Reports annotations written / without
             evidence / failed. Fails only if the mechanism breaks (an
             exception escapes, a decision is not committed, a partial
             annotation remains) - never on the counts. A different real
             review order gives different numbers; that is not an error.

Negative control (old container): D0, D1, D2, PINS, DATA pass; D3, RESOLVER,
LAYER and REHEARSAL fail.

Never writes to the live database (mode=ro); all write probes run on copies
in the work dir. Prints ids, counts and hashes only. Exit 0 = all PASS.
"""

import argparse
import hashlib
import importlib.util
import json
import re
import sqlite3
import sys
import warnings
from collections import Counter
from datetime import datetime
from pathlib import Path

PRECEDENT_TABLES = ("precedent_annotations", "precedent_annotation_evidence", "precedent_audit")
PRECEDENT_TRIGGERS = {f"trg_{t}_no_{op}" for t in PRECEDENT_TABLES for op in ("update", "delete")}
CUT_OFF_INDEX = "idx_precedent_layer_started"
NEW_FILES = ("precedent_store.py", "precedent_annotator.py", "precedent_report.py",
             "learning/__init__.py", "learning/precedent.py")
REHEARSAL_DECISION = "approved"
LABEL = re.compile(r"human_decision\s*=\s*\?")
RESOLVE = re.compile(r"SET\s+status\s*=\s*'resolved'")
OBS_DECISIONS = ("SELECT id, review_status, human_decision, corrected_category, reviewed_at, "
                 "review_reason, reviewed_by FROM classification_observations ORDER BY id")
ANNOTATION_INSERT = (
    "INSERT INTO precedent_annotations (id, observation_id, memory_type, annotation_trigger, "
    "device_id, classifier_name, hypothesis_category, result, evidence_count, sample_size, "
    "correction_count, outcome_distribution, evidence_maturity, evidence_as_of, "
    "evidence_digest, policy_version, created_at) "
    "VALUES (?, ?, ?, ?, 'd', 'c', 'h', ?, 0, 1, 0, '{}', 'insufficient', 't', 'x', 'd3', 't')")
OBS_REVIEW_FIELDS = ("review_status", "human_decision", "corrected_category", "reviewed_at",
                     "review_reason", "reviewed_by")
CASE_DECISION_FIELDS = ("status", "decision", "corrected_category", "decided_at")
CASE_MUTABLE_FIELDS = CASE_DECISION_FIELDS + ("updated_at", "last_observation_id")
AUDIT_INSERT = ("INSERT INTO precedent_audit (id, event, source, policy_version, created_at) "
                "VALUES (?, ?, ?, 'd3', 't')")


def _load_d2_verify():
    path = Path(__file__).with_name("d2_verify.py")
    spec = importlib.util.spec_from_file_location("d2_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


d2 = _load_d2_verify()


def _dicts(conn, sql: str, params=()) -> list:
    cursor = conn.execute(sql, params)
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _instant(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _expect_constraint(work, sql: str, params, kind: str, label: str) -> list:
    try:
        work.execute(sql, params)
        failure = [f"{label} was NOT refused"]
    except sqlite3.IntegrityError as exc:
        failure = [] if str(exc).startswith(f"{kind} constraint failed") else [
            f"{label}: unexpected message {exc}"]
    work.rollback()
    return failure


# --- D2 (STEP 7P triggers; the unexpected-trigger check moved to D3) ---------------

def check_d2(live, work_path: Path) -> list:
    missing = sorted(d2.STEP7P_TRIGGERS - d2._triggers(live))
    print(f"  STEP 7P triggers present: {5 - len(missing)}/5")
    failures = [f"missing trigger {n}" for n in missing]
    failures += [f"missing table {t}" for t in d2.APPEND_ONLY_TABLES if not d2._columns(live, t)]
    if "input_id" not in d2._columns(live, "classification_observations"):
        failures.append("missing column classification_observations.input_id")
    if failures:
        return failures
    work = d2._copy(live, work_path)
    work.execute("INSERT INTO classification_inputs VALUES "
                 "('inp_d3probe','d','f','v','{}','no_match','[]','s','t')")
    work.commit()
    checks = [("UPDATE classification_observations SET input_id = ? WHERE id = ?",
               ("inp_d3probe", d2._first_observation(work)), "IMMUTABLE_OBSERVATION_INPUT: "
               "classification_observations.input_id cannot be changed after insert", "input_id")]
    for table, column in (("classification_inputs", "outcome"),
                          ("classification_sweeps", "source")):
        checks.append((f"UPDATE {table} SET {column} = {column}", (),
                       f"APPEND_ONLY: {table} rows cannot be updated", f"{table} UPDATE"))
        checks.append((f"DELETE FROM {table}", (),
                       f"APPEND_ONLY: {table} rows cannot be deleted", f"{table} DELETE"))
    if not live.execute("SELECT COUNT(*) FROM classification_sweeps").fetchone()[0]:
        failures.append("no classification_sweeps row to test enforcement on")
    refused = [f for sql, p, msg, label in checks
               for f in d2._expect_refused(work, sql, p, msg, label)]
    work.close()
    print(f"  D2 protected changes refused with exact message: "
          f"{len(checks) - len(refused)}/{len(checks)}")
    return failures + refused


# --- D3 ------------------------------------------------------------------------------

def check_d3(live, work_path: Path) -> list:
    names = d2._triggers(live)
    missing = sorted(PRECEDENT_TRIGGERS - names)
    unexpected = sorted(names - d2.D1_TRIGGERS - d2.STEP7P_TRIGGERS - PRECEDENT_TRIGGERS)
    print(f"  STEP 7a triggers present: {6 - len(missing)}/6")
    print(f"  triggers in total: {len(names)} (expected 24); unexpected: {unexpected or 'none'}")
    failures = [f"missing trigger {n}" for n in missing]
    failures += [f"unexpected trigger {n}" for n in unexpected]
    failures += [f"missing table {t}" for t in PRECEDENT_TABLES if not d2._columns(live, t)]
    if not live.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                        (CUT_OFF_INDEX,)).fetchone():
        failures.append(f"missing index {CUT_OFF_INDEX}")
    if failures:
        return failures  # enforcement cannot be probed on an incomplete schema

    work = d2._copy(live, work_path)
    work.execute(ANNOTATION_INSERT, ("pan_d3probe", "obs_d3probe", "class_pattern",
                                     "observation", "ambiguous"))
    work.execute("INSERT INTO precedent_annotation_evidence VALUES "
                 "('pan_d3probe', NULL, 'obs_d3other', 'd2', 'approved', NULL, 't')")
    work.execute(AUDIT_INSERT, ("pau_d3probe", "annotation_failed", "observation"))
    work.execute("INSERT OR IGNORE INTO precedent_audit (id, event, policy_version, created_at) "
                 "VALUES ('pau_d3start', 'layer_started', 'd3', 't')")
    work.commit()
    refused = []
    for table in PRECEDENT_TABLES:
        for op, sql in (("updated", f"UPDATE {table} SET rowid = rowid"),
                        ("deleted", f"DELETE FROM {table}")):
            refused += d2._expect_refused(work, sql, (), f"APPEND_ONLY: {table} rows cannot be {op}",
                                          f"{table} {op}")
    print(f"  append-only refusals with exact message: {6 - len(refused)}/6")
    constraints = [
        (AUDIT_INSERT, ("pau_x", "layer_started", "observation"), "CHECK", "cut-off with a source"),
        (AUDIT_INSERT, ("pau_x", "annotation_failed", None), "CHECK", "failure without a source"),
        (AUDIT_INSERT, ("pau_x", "annotation_failed", "review"), "CHECK", "failure, unknown source"),
        (AUDIT_INSERT, ("pau_x", "something_else", None), "CHECK", "unknown audit event"),
        (AUDIT_INSERT, ("pau_x", "layer_started", None), "UNIQUE", "second layer_started"),
        (ANNOTATION_INSERT, ("pan_x", "obs_x", "combined", "observation", "ambiguous"),
         "CHECK", "unknown memory_type"),
        (ANNOTATION_INSERT, ("pan_x", "obs_x", "class_pattern", "annotation_failed", "ambiguous"),
         "CHECK", "unknown annotation_trigger"),
        (ANNOTATION_INSERT, ("pan_x", "obs_x", "class_pattern", "observation", "guess"),
         "CHECK", "unknown result"),
        (ANNOTATION_INSERT, ("pan_x", "obs_d3probe", "class_pattern", "observation", "ambiguous"),
         "UNIQUE", "second annotation of one observation and memory"),
    ]
    broken = [f for sql, params, kind, label in constraints
              for f in _expect_constraint(work, sql, params, kind, label)]
    work.close()
    print(f"  CHECK/UNIQUE constraints enforced: {len(constraints) - len(broken)}/{len(constraints)}")
    return refused + broken


# --- DATA ----------------------------------------------------------------------------

def _row_change(table: str, row: dict, now: dict, pre_obs: set) -> tuple:
    """(failure or None, 'advanced' | 'decided' | None) for one snapshot row.
    A human decision that exists in the snapshot is immutable; what was
    undecided may be decided once, through the review fields only."""
    if table == "classification_observations":
        raw = {k: v for k, v in row.items() if k not in OBS_REVIEW_FIELDS}
        if {k: now[k] for k in raw} != raw:
            return "raw fields changed", None
        if now == row:
            return None, None
        if row["human_decision"] is not None or now["human_decision"] is None:
            return "decision changed", None
        return None, "decided"
    if table != "review_cases":
        return (None if now == row else "changed"), None
    identity = {k: v for k, v in row.items() if k not in CASE_MUTABLE_FIELDS}
    if {k: now[k] for k in identity} != identity:
        return "identity changed", None
    decision = {k: row[k] for k in CASE_DECISION_FIELDS}
    decided = None
    if {k: now[k] for k in decision} != decision:
        if row["status"] != "pending" or now["status"] != "resolved":
            return "decision/identity changed", None
        decided = "decided"
    if now["last_observation_id"] != row["last_observation_id"]:
        if now["last_observation_id"] in pre_obs:
            return "pointer moved to old evidence", None
        return None, decided or "advanced"
    return None, decided


def check_data(pre, live) -> list:
    """Every pre_d3.db row is still present; nothing but a first human
    decision (and a case pointer advancing to new evidence) has changed."""
    failures = []
    tables = [r[0] for r in pre.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    pre_obs = set(d2._rows(pre, "classification_observations",
                           d2._columns(pre, "classification_observations")))
    for table in tables:
        cols = d2._columns(pre, table)
        before, after = d2._rows(pre, table, cols), d2._rows(live, table, cols)
        changes = Counter()
        for key, row in before.items():
            if key not in after:
                failures.append(f"{table}: row {key} missing")
                continue
            failure, change = _row_change(table, row, after[key], pre_obs)
            changes[change] += 1
            if failure:
                failures.append(f"{table}: {key} {failure}")
        print(f"  {table}: {len(before)} snapshot rows checked, "
              f"+{len(after) - len(before)} new, {changes['advanced']} pointer(s) advanced, "
              f"{changes['decided']} decided since the snapshot")
    return failures


# --- RESOLVER ------------------------------------------------------------------------

def _static_resolver(src_root: Path) -> list:
    hits = []
    for path in sorted((src_root / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        hits += [(path.name, kind, text, match.start())
                 for kind, pattern in (("label", LABEL), ("resolve", RESOLVE))
                 for match in pattern.finditer(text)]
    found = sorted((name, kind) for name, kind, _, _ in hits)
    print(f"  statements writing a decision: {found}")
    if found != [("storage.py", "label"), ("storage.py", "resolve")]:
        return ["decision writes are not exactly one label and one resolve in storage.py"]
    text = hits[0][2]
    start = text.find("    def resolve_review_case(")
    end = text.find("\n    def ", start + 1)
    if start < 0 or not all(start < position < end for _, _, _, position in hits):
        return ["a decision is written outside resolve_review_case()"]
    return []


def _add_superseded_probe(work) -> None:
    """A copy of the oldest observation under a new id, undecided: never
    the current evidence of any case."""
    columns = d2._columns(work, "classification_observations")
    reset = {"id": "'obs_d3probe'", "review_status": "'pending'", "human_decision": "NULL",
             "corrected_category": "NULL", "reviewed_at": "NULL", "review_reason": "NULL",
             "reviewed_by": "NULL"}
    work.execute(
        f"INSERT INTO classification_observations ({', '.join(columns)}) "
        f"SELECT {', '.join(reset.get(c, c) for c in columns)} FROM classification_observations "
        "ORDER BY created_at, id LIMIT 1")
    work.commit()


def _legacy_calls(storage, observation_id: str) -> list:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return [storage.approve_observation(observation_id),
                storage.reject_observation(observation_id),
                storage.correct_observation(observation_id, "d3_probe")]


def check_resolver(live, work_path: Path, src_root: Path) -> list:
    failures = _static_resolver(src_root)
    work = d2._copy(live, work_path)
    if d2._first_observation(work) is None:
        work.close()
        return failures + ["no observation to probe the legacy methods on"]
    _add_superseded_probe(work)
    work.close()
    sys.path.insert(0, str(src_root))
    from src.storage import Storage  # the deployed code, on the copy only
    storage = Storage(work_path)
    conn = storage.connect()
    stale = [r[0] for r in conn.execute(
        "SELECT o.id FROM classification_observations o WHERE o.human_decision IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM review_cases rc WHERE rc.last_observation_id = o.id "
        "AND rc.status = 'pending') ORDER BY o.id")]
    before = (conn.execute(OBS_DECISIONS).fetchall(),
              conn.execute("SELECT * FROM review_cases ORDER BY id").fetchall())
    results = [r for observation_id in stale for r in _legacy_calls(storage, observation_id)]
    refused = sum(r == "not_reviewable" for r in results)
    same = before == (conn.execute(OBS_DECISIONS).fetchall(),
                      conn.execute("SELECT * FROM review_cases ORDER BY id").fetchall())
    print(f"  observations that are not current evidence of a pending case: {len(stale)} "
          f"(1 synthetic); legacy calls refused as not_reviewable: {refused}/{len(results)}; "
          f"decision fields identical: {'yes' if same else 'NO'}")
    if refused != len(results) or bool(results) is False:
        failures.append("a legacy method did not refuse a non-current observation")
    if not same:
        failures.append("a refused legacy call changed decision fields")

    case = conn.execute("SELECT id, last_observation_id FROM review_cases "
                        "WHERE status = 'pending' ORDER BY id LIMIT 1").fetchone()
    if case is None:
        print("  no pending case: delegation not probed")
    else:
        result = _legacy_calls(storage, case[1])
        row = conn.execute("SELECT status, decision, decided_at FROM review_cases WHERE id = ?",
                           (case[0],)).fetchone()
        obs = conn.execute("SELECT human_decision, reviewed_at FROM classification_observations "
                           "WHERE id = ?", (case[1],)).fetchone()
        ok = (result == ["resolved", "already_resolved", "already_resolved"]
              and tuple(row) == ("resolved", "approved", obs[1]) and obs[0] == "approved")
        print(f"  legacy call on current evidence resolves through the resolver: "
              f"{'yes' if ok else 'NO'}")
        if not ok:
            failures.append(f"legacy delegation returned {[str(r) for r in result]}")
    conn.close()
    return failures


# --- LAYER ---------------------------------------------------------------------------

def _digest(evidence: list, policy_version: str) -> str:
    rows = sorted(([e["labelled_observation_id"], e["device_id"], e["human_decision"],
                    e["corrected_category"], e["decided_at"], e["review_case_id"]]
                   for e in evidence), key=lambda row: row[0])
    canonical = json.dumps({"policy_version": policy_version, "evidence": rows},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_annotation(a: dict, evidence: list, observations: dict, cut_off: str) -> list:
    failures, where = [], a["id"]
    observation = observations.get(a["observation_id"])
    if observation is None:
        return [f"{where}: annotated observation does not exist"]
    if len(evidence) != a["sample_size"] or not evidence:
        failures.append(f"{where}: {len(evidence)} evidence rows for sample_size {a['sample_size']}")
    elif _digest(evidence, a["policy_version"]) != a["evidence_digest"]:
        failures.append(f"{where}: evidence digest does not match")
    created = _instant(a["created_at"])
    if created < _instant(cut_off):
        failures.append(f"{where}: written before the cut-off")
    if observation["reviewed_at"] and created >= _instant(observation["reviewed_at"]):
        failures.append(f"{where}: not written before the decision on its observation")
    old = _instant(observation["created_at"]) < _instant(cut_off)
    if old != (a["annotation_trigger"] == "bootstrap_pending"):
        failures.append(f"{where}: trigger {a['annotation_trigger']} on an observation "
                        f"{'older' if old else 'newer'} than the cut-off")
    own = a["memory_type"] == "device_precedent"
    for e in evidence:
        source = observations.get(e["labelled_observation_id"], {})
        if (source.get("human_decision"), source.get("corrected_category"),
                source.get("reviewed_at")) != (e["human_decision"], e["corrected_category"],
                                               e["decided_at"]):
            failures.append(f"{where}: evidence differs from the labelled observation")
        if _instant(e["decided_at"]) >= created:
            failures.append(f"{where}: evidence decided after the annotation")
        if (e["device_id"] == a["device_id"]) != own:
            failures.append(f"{where}: {a['memory_type']} evidence from the wrong device")
    return failures


def check_layer(live, options: Path) -> list:
    if not all(d2._columns(live, t) for t in PRECEDENT_TABLES):
        return ["precedent tables absent"]
    option = "off"
    if options.exists():
        option = json.loads(options.read_text(encoding="utf-8")).get("precedent_mode", "off")
    audit = Counter((r["event"], r["source"]) for r in _dicts(live, "SELECT * FROM precedent_audit"))
    annotations = _dicts(live, "SELECT * FROM precedent_annotations ORDER BY created_at, id")
    started = [r[0] for r in live.execute(
        "SELECT created_at FROM precedent_audit WHERE event = 'layer_started'")]
    print(f"  option precedent_mode: {option}")
    print(f"  layer_started rows: {len(started)}"
          + (f" (cut-off {started[0]})" if started else " (layer never started)"))
    print(f"  annotations: {len(annotations)}; annotation_failed: observation "
          f"{audit[('annotation_failed', 'observation')]}, bootstrap_pending "
          f"{audit[('annotation_failed', 'bootstrap_pending')]}")
    duplicates = live.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM classification_observations "
        "WHERE human_decision IS NOT NULL GROUP BY device_id, classifier_name, "
        "hypothesis_category HAVING COUNT(*) > 1)").fetchone()[0]
    print(f"  logical keys with more than one labelled observation: {duplicates} (must be 0)")
    integrity = [f"{duplicates} logical key(s) with more than one labelled observation: every "
                 "annotation for their classifier/category would fail"] if duplicates else []
    if not started:
        failures = integrity + ([] if option != "shadow" else [
            "option is shadow but the layer never started (no layer_started row)"])
        if annotations or sum(audit.values()):
            failures.append("precedent rows exist although the layer never started")
        return failures
    if len(started) != 1:
        return [f"{len(started)} layer_started rows"]

    observations = {o["id"]: o for o in _dicts(live, "SELECT * FROM classification_observations")}
    evidence = {}
    for e in _dicts(live, "SELECT * FROM precedent_annotation_evidence"):
        evidence.setdefault(e["annotation_id"], []).append(e)
    failures = integrity + [f"evidence rows of unknown annotation {i}"
                            for i in sorted(set(evidence) - {a["id"] for a in annotations})]
    for a in annotations:
        failures += _check_annotation(a, evidence.get(a["id"], []), observations, started[0])
    since = [o for o in observations.values() if _instant(o["created_at"]) >= _instant(started[0])]
    by = Counter((a["memory_type"], a["annotation_trigger"]) for a in annotations)
    print(f"  observations since the cut-off: {len(since)}; annotations by memory/trigger: "
          f"{dict(sorted(by.items())) or 'none'}")
    print(f"  annotations fully consistent: {len(annotations) - len({f.split(':')[0] for f in failures})}"
          f"/{len(annotations)}")
    return failures


# --- REHEARSAL -----------------------------------------------------------------------

def _rehearse_case(storage, conn, case: dict) -> tuple:
    """Resolve one case on the copy. Returns (outcome, failures) where
    outcome is 'annotated' | 'no_evidence' | 'failed'."""
    case_id, observation_id = case["case_id"], case["observation_id"]
    try:
        result = storage.resolve_review_case(case_id, observation_id, REHEARSAL_DECISION)
    except Exception as exc:
        return "failed", [f"{case_id}: exception escaped the resolver: {exc!r}"]
    row = conn.execute("SELECT rc.status, rc.decided_at, o.human_decision FROM review_cases rc "
                       "JOIN classification_observations o ON o.id = ? WHERE rc.id = ?",
                       (observation_id, case_id)).fetchone()
    failures = []
    if result != "resolved" or (row[0], row[2]) != ("resolved", REHEARSAL_DECISION):
        failures.append(f"{case_id}: decision not committed (result {result!r})")
    annotations = _dicts(conn, "SELECT * FROM precedent_annotations WHERE observation_id = ?",
                         (observation_id,))
    failed = conn.execute("SELECT error_class FROM precedent_audit WHERE event = "
                          "'annotation_failed' AND observation_id = ?", (observation_id,)).fetchall()
    for a in annotations:
        rows = conn.execute("SELECT COUNT(*) FROM precedent_annotation_evidence "
                            "WHERE annotation_id = ?", (a["id"],)).fetchone()[0]
        if rows != a["sample_size"] or (failed and annotations):
            failures.append(f"{case_id}: partial annotation {a['id']}")
        if row[1] and _instant(a["created_at"]) >= _instant(row[1]):
            failures.append(f"{case_id}: annotation not before the decision")
    detail = ", ".join(
        f"{a['memory_type']} {a['result']} evidence {a['evidence_count']}/{a['sample_size']} "
        f"{a['evidence_maturity']} n={a['sample_size']}" for a in annotations)
    outcome = "failed" if failed else "annotated" if annotations else "no_evidence"
    print(f"    {case_id} {case['classifier_name']}/{case['hypothesis_category']}: "
          + (f"annotation FAILED ({failed[0][0]})" if failed else detail or "no earlier evidence"))
    return outcome, failures


def check_rehearsal(live, work_path: Path, src_root: Path) -> list:
    print("  FORECAST - not a gate invariant. Live database untouched.")
    d2._copy(live, work_path).close()
    sys.path.insert(0, str(src_root))
    from src.storage import Storage  # the deployed code, on the copy only
    storage = Storage(work_path, precedent_mode="shadow")
    storage.ensure_precedent_layer_started()
    queue = storage.get_pending_review_cases(limit=100000)
    print(f"  pending cases: {len(queue)}; order: as the `review` CLI lists them; "
          f"hypothetical decision for each: {REHEARSAL_DECISION}")
    print("  (which annotations get written does not depend on the decisions chosen; "
          "the suggested outcomes do)")
    conn = storage.connect()
    outcomes, failures = Counter(), []
    for case in queue:
        outcome, problems = _rehearse_case(storage, conn, case)
        outcomes[outcome] += 1
        failures += problems
    left = conn.execute("SELECT COUNT(*) FROM review_cases WHERE status = 'pending'").fetchone()[0]
    if left:
        failures.append(f"{left} case(s) still pending after the rehearsal")
    print(f"  forecast for this order: annotations written for {outcomes['annotated']} case(s), "
          f"without evidence {outcomes['no_evidence']}, annotation_failed {outcomes['failed']}")
    conn.close()
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src-root", default="/app")
    parser.add_argument("--live-db", default="/data/core.db")
    parser.add_argument("--pre-db", default="/tmp/pre_d3.db")
    parser.add_argument("--work-dir", default="/tmp")
    parser.add_argument("--requirements", default="/share/d3/requirements.txt")
    parser.add_argument("--options", default="/data/options.json")
    args = parser.parse_args()
    src_root, work_dir, pre_p = Path(args.src_root), Path(args.work_dir), Path(args.pre_db)
    options = Path(args.options)

    print("D3 VERIFY (cumulative D0 + D1 + D2 + D3, STEP 7a)")
    for name in d2.DEPLOYED_FILES + NEW_FILES:
        print(f"deployed {name:<24} {d2.sha256(src_root / 'src' / name)}")
    print(f"pre_d3.db SHA256 {d2.sha256(pre_p)}")
    print(f"options {options.read_text(encoding='utf-8').strip() if options.exists() else 'absent'}")
    if not pre_p.exists():
        print(f"\nRESULT: FAIL - snapshot {pre_p} not found (take the backup first)")
        return 1

    live = sqlite3.connect(f"file:{args.live_db}?mode=ro", uri=True)
    pre = sqlite3.connect(f"file:{pre_p}?mode=ro", uri=True)
    sections = [
        ("D0", lambda: d2.check_d0(live, work_dir / "d3_verify_d0.db", src_root)),
        ("D1", lambda: d2.check_d1(live, work_dir / "d3_verify_d1.db")),
        ("D2", lambda: check_d2(live, work_dir / "d3_verify_d2.db")),
        ("D3", lambda: check_d3(live, work_dir / "d3_verify_d3.db")),
        ("PINS", lambda: d2.check_pins(Path(args.requirements))),
        ("DATA", lambda: check_data(pre, live)),
        ("RESOLVER", lambda: check_resolver(live, work_dir / "d3_verify_resolver.db", src_root)),
        ("LAYER", lambda: check_layer(live, options)),
        ("REHEARSAL", lambda: check_rehearsal(live, work_dir / "d3_verify_rehearsal.db", src_root)),
    ]
    results = {}
    for name, run in sections:
        print(f"\n[{name}]")
        try:
            results[name] = run()
        except Exception as exc:  # a crashing check is a failed check
            results[name] = [f"check raised {exc!r}"]
    live.close()
    pre.close()

    print()
    for name, failures in results.items():
        print(f"{name:<9} {'PASS' if not failures else 'FAIL'}")
        for failure in failures:
            print(f"  - {failure}")
    ok = not any(results.values())
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
