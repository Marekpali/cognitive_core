"""
D2 production verification - cumulative: D0 + D1 + D2 (STEP 7P) + data
preservation + shadow state.

Run INSIDE the container with the pre-deployment backup at /tmp/pre_d2.db:

    docker exec app_local_cognitive_core python3 /tmp/d2_verify.py

Each section reports PASS/FAIL on its own, so the negative control (run in
the OLD container before deployment) must show D0 PASS, D1 PASS and D2 FAIL
only for missing STEP 7P elements.

  D0        on a backup-API copy, with the DEPLOYED code: re-resolving every
            resolved case and every legacy decision call returns
            'already_resolved'; all decision fields identical before/after.
  D1        the 13 D1 triggers exist; on a copy each protected change is
            refused with the exact message; a review-only UPDATE is allowed.
  D2        the 5 STEP 7P triggers, tables and input_id column exist; no
            unexpected trigger; on a copy input_id changes and UPDATE/DELETE
            on the append-only tables are refused with the exact message.
  DATA      every pre_d2.db row is still present and identical (pre-D2
            columns); pre-D2 observations keep input_id NULL; review_cases
            may only advance last_observation_id/updated_at to new evidence.
  SHADOW    no classification_inputs, no new observations, review cases or
            assets since the snapshot; every sweep row is mode 'shadow'.

Never writes to the live database (mode=ro); all write probes run on
copies in /tmp. Prints ids, counts and hashes only. Exit 0 = all PASS.
"""

import argparse
import hashlib
import sqlite3
import sys
import warnings
from pathlib import Path

D1_RAW_COLUMNS = (
    "observation_group_id", "device_id", "classifier_name",
    "hypothesis_category", "hypothesis_confidence", "hypothesis_reasoning",
    "device_name", "device_model", "device_manufacturer",
    "device_source_adapter", "device_entity_count", "created_at",
)
D1_TRIGGERS = {f"trg_obs_immutable_{c}" for c in D1_RAW_COLUMNS} | {"trg_obs_immutable_id"}
APPEND_ONLY_TABLES = ("classification_inputs", "classification_sweeps")
STEP7P_TRIGGERS = {"trg_obs_immutable_input_id"} | {
    f"trg_{t}_no_{op}" for t in APPEND_ONLY_TABLES for op in ("update", "delete")}
CHANGED = {"hypothesis_confidence": -1.0, "device_entity_count": -1}
CASE_POINTER_FIELDS = {"last_observation_id", "updated_at"}
CASE_FIELDS = "status, decision, corrected_category, decided_at, updated_at, last_observation_id"
OBS_DECISION_FIELDS = ("id, review_status, human_decision, corrected_category, "
                       "reviewed_at, review_reason, reviewed_by")
OTHER_DECISION = {"approved": "rejected", "rejected": "approved", "corrected": "approved"}
DEPLOYED_FILES = (
    "storage.py", "core.py", "main.py", "ha_sensor.py", "coverage_store.py",
    "observation_input.py", "options.py", "sweep.py", "sweep_scheduler.py",
    "adapters/ha.py", "classifiers/__init__.py",
)
SHADOW_COUNTS = ("classification_observations", "review_cases", "assets")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "absent"


def _copy(live: sqlite3.Connection, path: Path) -> sqlite3.Connection:
    path.unlink(missing_ok=True)
    work = sqlite3.connect(path)
    live.backup(work)
    return work


def _columns(conn, table) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _triggers(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}


def _expect_refused(work, sql, params, message, label) -> list:
    try:
        work.execute(sql, params)
        failure = [f"{label} was NOT refused"]
    except sqlite3.IntegrityError as exc:
        failure = [] if str(exc) == message else [f"{label}: unexpected message {exc}"]
    work.rollback()
    return failure


def _first_observation(work):
    row = work.execute("SELECT id FROM classification_observations "
                       "ORDER BY created_at, id LIMIT 1").fetchone()
    return row[0] if row else None


# --- D0 --------------------------------------------------------------------------

def _decision_snapshot(conn) -> dict:
    conn.row_factory = sqlite3.Row
    snap = {}
    for case in conn.execute("SELECT id, device_id, classifier_name, hypothesis_category "
                             "FROM review_cases WHERE status = 'resolved' ORDER BY id"):
        fields = conn.execute(f"SELECT {CASE_FIELDS} FROM review_cases WHERE id = ?",
                              (case["id"],)).fetchone()
        labelled = conn.execute(
            f"SELECT {OBS_DECISION_FIELDS} FROM classification_observations "
            "WHERE device_id = ? AND classifier_name = ? AND hypothesis_category = ? "
            "AND human_decision IS NOT NULL ORDER BY id",
            (case["device_id"], case["classifier_name"], case["hypothesis_category"])).fetchall()
        snap[case["id"]] = (dict(fields), [dict(r) for r in labelled])
    conn.row_factory = None
    return snap


def check_d0(live, work_path: Path, src_root: Path) -> list:
    work = _copy(live, work_path)
    before = _decision_snapshot(work)
    work.close()
    if not before:
        return ["no resolved cases found - nothing was verified"]
    sys.path.insert(0, str(src_root))
    from src.storage import Storage  # the deployed code, on the copy only
    storage = Storage(work_path)
    failures = []
    for case_id, (case, labelled) in before.items():
        result = storage.resolve_review_case(
            case_id, expected_observation_id=case["last_observation_id"],
            decision=OTHER_DECISION[case["decision"]])
        if result != "already_resolved":
            failures.append(f"{case_id}: resolve_review_case returned {result!r}")
        for row in labelled:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                legacy = (storage.approve_observation(row["id"])
                          if row["human_decision"] != "approved"
                          else storage.reject_observation(row["id"]))
            if legacy != "already_resolved":
                failures.append(f"{row['id']}: legacy decision returned {legacy!r}")
    storage.connection.close()
    after = _decision_snapshot(sqlite3.connect(work_path))
    if after != before:
        failures.append("decision fields changed after overwrite attempts")
    print(f"  resolved cases re-decided on a copy: {len(before)}; "
          f"decision fields identical: {'yes' if after == before else 'NO'}")
    return failures


# --- D1 --------------------------------------------------------------------------

def check_d1(live, work_path: Path) -> list:
    missing = sorted(D1_TRIGGERS - _triggers(live))
    print(f"  D1 triggers present: {13 - len(missing)}/13")
    failures = [f"missing trigger {n}" for n in missing]
    work = _copy(live, work_path)
    obs = _first_observation(work)
    if obs is None:
        work.close()
        return failures + ["no observation to test enforcement on"]
    checks = [(f"UPDATE classification_observations SET {c} = ? WHERE id = ?",
               (CHANGED.get(c, "d2_probe"), obs),
               f"IMMUTABLE_RAW_HYPOTHESIS: classification_observations.{c} "
               "cannot be changed after insert", c) for c in D1_RAW_COLUMNS]
    checks.append(("UPDATE classification_observations SET id = ? WHERE id = ?",
                   ("d2_probe", obs), "IMMUTABLE_OBSERVATION_IDENTITY: "
                   "classification_observations.id cannot be changed after insert", "id"))
    refused = [f for sql, p, msg, label in checks for f in _expect_refused(work, sql, p, msg, label)]
    print(f"  D1 protected changes refused with exact message: {13 - len(refused)}/13")
    work.execute("UPDATE classification_observations SET review_reason = 'd2_probe' "
                 "WHERE id = ?", (obs,))
    work.rollback()
    work.close()
    print("  review-only UPDATE allowed: yes")
    return failures + refused


# --- D2 --------------------------------------------------------------------------

def check_d2(live, work_path: Path) -> list:
    names = _triggers(live)
    missing = sorted(STEP7P_TRIGGERS - names)
    unexpected = sorted(names - D1_TRIGGERS - STEP7P_TRIGGERS)
    print(f"  STEP 7P triggers present: {5 - len(missing)}/5")
    print(f"  unexpected triggers: {unexpected or 'none'}")
    failures = [f"missing trigger {n}" for n in missing]
    failures += [f"unexpected trigger {n}" for n in unexpected]
    failures += [f"missing table {t}" for t in APPEND_ONLY_TABLES if not _columns(live, t)]
    if "input_id" not in _columns(live, "classification_observations"):
        failures.append("missing column classification_observations.input_id")
    if failures:
        return failures  # enforcement cannot be probed on an incomplete schema

    work = _copy(live, work_path)
    obs = _first_observation(work)
    work.execute("INSERT INTO classification_inputs VALUES "
                 "('inp_d2probe','d','f','v','{}','no_match','[]','s','t')")
    work.execute("INSERT INTO classification_sweeps (id, mode, source, "
                 "classifier_set_version, started_at, finished_at, discovered, "
                 "skipped_no_entities, skipped_missing_metadata, unchanged, classified, "
                 "no_match, error, excluded_disabled_entities, unavailable_entities, "
                 "devices_json) VALUES ('swp_d2probe','shadow','d2','v','t','t',"
                 "0,0,0,0,0,0,0,0,0,'{}')")
    work.commit()
    checks = [("UPDATE classification_observations SET input_id = ? WHERE id = ?",
               ("inp_d2probe", obs), "IMMUTABLE_OBSERVATION_INPUT: "
               "classification_observations.input_id cannot be changed after insert",
               "input_id")]
    for table, column in (("classification_inputs", "outcome"),
                          ("classification_sweeps", "source")):
        checks.append((f"UPDATE {table} SET {column} = {column}", (),
                       f"APPEND_ONLY: {table} rows cannot be updated", f"{table} UPDATE"))
        checks.append((f"DELETE FROM {table}", (),
                       f"APPEND_ONLY: {table} rows cannot be deleted", f"{table} DELETE"))
    refused = [f for sql, p, msg, label in checks for f in _expect_refused(work, sql, p, msg, label)]
    work.close()
    print(f"  D2 protected changes refused with exact message: "
          f"{len(checks) - len(refused)}/{len(checks)}")
    return refused


# --- DATA ------------------------------------------------------------------------

def _rows(conn, table, cols) -> dict:
    key = "id" if "id" in cols else "rowid"
    return {r[0]: dict(zip(cols, r[1:])) for r in conn.execute(
        f"SELECT {key}, {', '.join(cols)} FROM {table}")}


def check_data(pre, live) -> list:
    failures = []
    tables = [r[0] for r in pre.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    pre_obs = set(_rows(pre, "classification_observations",
                        _columns(pre, "classification_observations")))
    for table in tables:
        cols = _columns(pre, table)
        before, after = _rows(pre, table, cols), _rows(live, table, cols)
        advanced = 0
        for key, row in before.items():
            now = after.get(key)
            if now is None:
                failures.append(f"{table}: row {key} missing")
            elif table == "review_cases":
                stable = {k: v for k, v in row.items() if k not in CASE_POINTER_FIELDS}
                if {k: now[k] for k in stable} != stable:
                    failures.append(f"review_cases: {key} decision/identity changed")
                elif now["last_observation_id"] != row["last_observation_id"]:
                    if now["last_observation_id"] in pre_obs:
                        failures.append(f"review_cases: {key} pointer moved to old evidence")
                    advanced += 1
            elif now != row:
                failures.append(f"{table}: row {key} changed")
        print(f"  {table}: {len(before)} snapshot rows checked, "
              f"+{len(after) - len(before)} new, {advanced} pointer(s) advanced")
    if pre_obs and "input_id" in _columns(live, "classification_observations"):
        marks = ",".join("?" for _ in pre_obs)
        linked = live.execute(
            f"SELECT COUNT(*) FROM classification_observations "
            f"WHERE input_id IS NOT NULL AND id IN ({marks})", tuple(pre_obs)).fetchone()[0]
        print(f"  pre-D2 observations with input_id: {linked} (must be 0)")
        if linked:
            failures.append(f"{linked} pre-D2 observation(s) gained an input_id")
    return failures


# --- SHADOW ----------------------------------------------------------------------

def check_shadow(pre, live) -> list:
    failures = []
    for table in SHADOW_COUNTS:
        before = pre.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        now = live.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {before} in snapshot, {now} live (must be equal)")
        if now != before:
            failures.append(f"shadow: {now - before} new {table} row(s)")
    if not _columns(live, "classification_inputs"):
        print("  classification_inputs: table absent")
        return failures
    inputs = live.execute("SELECT COUNT(*) FROM classification_inputs").fetchone()[0]
    print(f"  classification_inputs rows: {inputs} (must be 0)")
    if inputs:
        failures.append(f"shadow: {inputs} classification_inputs row(s) written")
    for row in live.execute(
            "SELECT id, mode, source, discovered, skipped_no_entities, "
            "skipped_missing_metadata, unchanged, classified, no_match, error "
            "FROM classification_sweeps ORDER BY rowid"):
        print("  sweep " + " ".join(str(v) for v in row))
        if row[1] != "shadow":
            failures.append(f"sweep {row[0]} ran in mode {row[1]}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src-root", default="/app")
    parser.add_argument("--live-db", default="/data/core.db")
    parser.add_argument("--pre-db", default="/tmp/pre_d2.db")
    parser.add_argument("--work-dir", default="/tmp")
    args = parser.parse_args()
    src_root, work_dir = Path(args.src_root), Path(args.work_dir)
    pre_p = Path(args.pre_db)

    print("D2 VERIFY (cumulative D0 + D1 + D2)")
    for name in DEPLOYED_FILES:
        print(f"deployed {name:<24} {sha256(src_root / 'src' / name)}")
    print(f"pre_d2.db SHA256 {sha256(pre_p)}")

    live = sqlite3.connect(f"file:{args.live_db}?mode=ro", uri=True)
    pre = sqlite3.connect(f"file:{pre_p}?mode=ro", uri=True)
    sections = [
        ("D0", lambda: check_d0(live, work_dir / "d2_verify_d0.db", src_root)),
        ("D1", lambda: check_d1(live, work_dir / "d2_verify_d1.db")),
        ("D2", lambda: check_d2(live, work_dir / "d2_verify_d2.db")),
        ("DATA", lambda: check_data(pre, live)),
        ("SHADOW", lambda: check_shadow(pre, live)),
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
        print(f"{name:<7} {'PASS' if not failures else 'FAIL'}")
        for failure in failures:
            print(f"  - {failure}")
    ok = not any(results.values())
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
