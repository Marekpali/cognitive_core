"""
D2 production verification: STEP 7P schema and shadow-mode start.

Run INSIDE the rebuilt container, after D2, with the pre-deployment backup
copied to /tmp/pre_d2.db:

    docker exec app_local_cognitive_core python3 /tmp/d2_verify.py

Never writes to the live database. Checks:
  1. triggers in the live DB (read-only): exactly the 13 D1 triggers plus
     the 5 STEP 7P triggers; a missing OR unexpected trigger fails;
  2. schema: classification_inputs, classification_sweeps and
     classification_observations.input_id exist;
  3. every row present in pre_d2.db is still present and identical in the
     live DB (pre-D2 columns compared; the new input_id column must be NULL
     on every pre-D2 observation). New rows are allowed; in review_cases
     only last_observation_id/updated_at may change, only to point at an
     observation that did not exist in the snapshot;
  4. shadow state: no classification_inputs rows and no observation newer
     than the snapshot; sweep rows are listed as counts only;
  5. on a backup-API copy in /tmp: every protected change is refused with
     the exact message, a review-only UPDATE is allowed.

Prints ids, counts and hashes only - never device names. Exit 0 = PASS.
"""

import argparse
import hashlib
import sqlite3
import sys
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
CASE_POINTER_FIELDS = {"last_observation_id", "updated_at"}
CHANGED = {"hypothesis_confidence": -1.0, "device_entity_count": -1}
DEPLOYED_FILES = (
    "storage.py", "core.py", "main.py", "ha_sensor.py", "coverage_store.py",
    "observation_input.py", "options.py", "sweep.py", "sweep_scheduler.py",
    "adapters/ha.py", "classifiers/__init__.py",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_triggers(live: sqlite3.Connection) -> list:
    names = {r[0] for r in live.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    print(f"D1 triggers: {len(names & D1_TRIGGERS)}/13")
    print(f"STEP 7P triggers: {len(names & STEP7P_TRIGGERS)}/5")
    for name in sorted(names & STEP7P_TRIGGERS):
        print(f"  {name}")
    failures = [f"missing trigger {n}" for n in sorted((D1_TRIGGERS | STEP7P_TRIGGERS) - names)]
    unexpected = sorted(names - D1_TRIGGERS - STEP7P_TRIGGERS)
    failures += [f"unexpected trigger {n}" for n in unexpected]
    print(f"unexpected triggers: {unexpected or 'none'}")
    return failures


def _columns(conn: sqlite3.Connection, table: str) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def check_schema(live: sqlite3.Connection) -> list:
    failures = [f"missing table {t}" for t in APPEND_ONLY_TABLES if not _columns(live, t)]
    if "input_id" not in _columns(live, "classification_observations"):
        failures.append("missing column classification_observations.input_id")
    print(f"schema: {'OK' if not failures else 'INCOMPLETE'}")
    return failures


def _rows(conn: sqlite3.Connection, table: str, cols: list) -> dict:
    key = "id" if "id" in cols else "rowid"
    return {r[0]: dict(zip(cols, r[1:])) for r in conn.execute(
        f"SELECT {key}, {', '.join(cols)} FROM {table}")}


def check_rows_preserved(pre: sqlite3.Connection, live: sqlite3.Connection) -> list:
    failures = []
    tables = [r[0] for r in pre.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    pre_obs = set(_rows(pre, "classification_observations",
                        _columns(pre, "classification_observations")))
    for table in tables:
        cols = _columns(pre, table)
        before, after = _rows(pre, table, cols), _rows(live, table, cols)
        changed = 0
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
                    changed += 1
            elif now != row:
                failures.append(f"{table}: row {key} changed")
        print(f"  {table}: {len(before)} snapshot rows checked, "
              f"+{len(after) - len(before)} new, {changed} pointer(s) advanced")
    if pre_obs:
        marks = ",".join("?" for _ in pre_obs)
        linked = live.execute(
            f"SELECT COUNT(*) FROM classification_observations "
            f"WHERE input_id IS NOT NULL AND id IN ({marks})", tuple(pre_obs)).fetchone()[0]
        print(f"  pre-D2 observations with input_id: {linked} (must be 0)")
        if linked:
            failures.append(f"{linked} pre-D2 observation(s) gained an input_id")
    return failures


def check_shadow_state(pre: sqlite3.Connection, live: sqlite3.Connection) -> list:
    failures = []
    inputs = live.execute("SELECT COUNT(*) FROM classification_inputs").fetchone()[0]
    obs_pre = pre.execute("SELECT COUNT(*) FROM classification_observations").fetchone()[0]
    obs_live = live.execute("SELECT COUNT(*) FROM classification_observations").fetchone()[0]
    print(f"  classification_inputs rows: {inputs} (shadow: must be 0)")
    print(f"  observations: {obs_pre} in snapshot, {obs_live} live (shadow: must be equal)")
    if inputs:
        failures.append(f"shadow: {inputs} classification_inputs row(s) written")
    if obs_live != obs_pre:
        failures.append(f"shadow: {obs_live - obs_pre} observation(s) written")
    for row in live.execute(
            "SELECT id, mode, source, discovered, skipped_no_entities, "
            "skipped_missing_metadata, unchanged, classified, no_match, error "
            "FROM classification_sweeps ORDER BY rowid"):
        print("  sweep " + " ".join(str(v) for v in row))
        if row[1] != "shadow":
            failures.append(f"sweep {row[0]} ran in mode {row[1]}")
    return failures


def _expect_refused(work, sql, params, message, label) -> list:
    try:
        work.execute(sql, params)
        failure = [f"{label} was NOT refused"]
    except sqlite3.IntegrityError as exc:
        failure = [] if str(exc) == message else [f"{label}: unexpected message {exc}"]
    work.rollback()
    return failure


def check_enforcement(work: sqlite3.Connection) -> list:
    obs = work.execute("SELECT id FROM classification_observations "
                       "ORDER BY created_at, id LIMIT 1").fetchone()
    if obs is None:
        return ["no observation to test enforcement on"]
    work.execute("INSERT INTO classification_inputs VALUES "
                 "('inp_d2probe','d','f','v','{}','no_match','[]','s','t')")
    work.execute("INSERT INTO classification_sweeps (id, mode, source, "
                 "classifier_set_version, started_at, finished_at, discovered, "
                 "skipped_no_entities, skipped_missing_metadata, unchanged, classified, "
                 "no_match, error, excluded_disabled_entities, unavailable_entities, "
                 "devices_json) VALUES ('swp_d2probe','shadow','d2','v','t','t',"
                 "0,0,0,0,0,0,0,0,0,'{}')")
    work.commit()

    checks = [(f"UPDATE classification_observations SET {c} = ? WHERE id = ?",
               (CHANGED.get(c, "d2_probe"), obs[0]),
               f"IMMUTABLE_RAW_HYPOTHESIS: classification_observations.{c} "
               "cannot be changed after insert", c) for c in D1_RAW_COLUMNS]
    checks.append(("UPDATE classification_observations SET id = ? WHERE id = ?",
                   ("d2_probe", obs[0]), "IMMUTABLE_OBSERVATION_IDENTITY: "
                   "classification_observations.id cannot be changed after insert", "id"))
    checks.append(("UPDATE classification_observations SET input_id = ? WHERE id = ?",
                   ("inp_d2probe", obs[0]), "IMMUTABLE_OBSERVATION_INPUT: "
                   "classification_observations.input_id cannot be changed after insert",
                   "input_id"))
    for table in APPEND_ONLY_TABLES:
        checks.append((f"UPDATE {table} SET source = source WHERE 1" if table.endswith("sweeps")
                       else f"UPDATE {table} SET outcome = outcome WHERE 1", (),
                       f"APPEND_ONLY: {table} rows cannot be updated", f"{table} UPDATE"))
        checks.append((f"DELETE FROM {table}", (),
                       f"APPEND_ONLY: {table} rows cannot be deleted", f"{table} DELETE"))

    failures = []
    for sql, params, message, label in checks:
        failures += _expect_refused(work, sql, params, message, label)
    print(f"  protected changes refused with exact message: "
          f"{len(checks) - len(failures)}/{len(checks)}")
    work.execute("UPDATE classification_observations SET review_reason = 'd2_probe' "
                 "WHERE id = ?", (obs[0],))
    work.rollback()
    print("  review-only UPDATE allowed: yes")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src-root", default="/app")
    parser.add_argument("--live-db", default="/data/core.db")
    parser.add_argument("--pre-db", default="/tmp/pre_d2.db")
    parser.add_argument("--work-db", default="/tmp/d2_verify.db")
    args = parser.parse_args()
    src, live_p = Path(args.src_root) / "src", Path(args.live_db)
    pre_p, work_p = Path(args.pre_db), Path(args.work_db)

    print("D2 VERIFY")
    for name in DEPLOYED_FILES:
        print(f"deployed {name:<24} {sha256(src / name)}")
    print(f"pre_d2.db SHA256 {sha256(pre_p)}\n")

    live = sqlite3.connect(f"file:{live_p}?mode=ro", uri=True)
    pre = sqlite3.connect(f"file:{pre_p}?mode=ro", uri=True)
    failures = check_triggers(live) + check_schema(live)
    if not failures:
        print("\nRows from pre_d2.db in the live database:")
        failures += check_rows_preserved(pre, live)
        print("\nShadow state:")
        failures += check_shadow_state(pre, live)
    pre.close()

    if not failures:
        work_p.unlink(missing_ok=True)
        work = sqlite3.connect(work_p)
        live.backup(work)
        print("\nEnforcement (on a copy):")
        failures += check_enforcement(work)
        work.close()
    live.close()

    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'}")
    for failure in failures:
        print(f"  - {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
