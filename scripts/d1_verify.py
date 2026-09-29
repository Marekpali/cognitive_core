"""
D1 production verification: M1 database-level immutability.

Run INSIDE the rebuilt container, after D1, with the pre-deployment backup
copied to /tmp/pre_d1.db:

    docker exec app_local_cognitive_core python3 /tmp/d1_verify.py

Never writes to the live database. Checks:
  1. triggers in the live DB (read-only): exactly the 12 raw-hypothesis
     triggers plus the observation-identity trigger, listed by name and
     group; a missing OR unexpected trigger fails;
  2. every row present in pre_d1.db is still present and identical in the
     live DB. New rows created after the snapshot are allowed. In
     review_cases, only last_observation_id/updated_at may change, and only
     to point at an observation that did not exist in the snapshot;
  3. on a backup-API copy in /tmp: changing each protected column is
     refused with the exact message, a review-only UPDATE is allowed.

Prints ids, names and hashes only. Exit 0 = PASS, 1 = FAIL.
"""

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

RAW_COLUMNS = (
    "observation_group_id", "device_id", "classifier_name",
    "hypothesis_category", "hypothesis_confidence", "hypothesis_reasoning",
    "device_name", "device_model", "device_manufacturer",
    "device_source_adapter", "device_entity_count", "created_at",
)
RAW_TRIGGERS = {f"trg_obs_immutable_{c}": c for c in RAW_COLUMNS}
IDENTITY_TRIGGER = "trg_obs_immutable_id"
CASE_POINTER_FIELDS = {"last_observation_id", "updated_at"}
CHANGED = {"hypothesis_confidence": -1.0, "device_entity_count": -1}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_triggers(live: sqlite3.Connection) -> list:
    names = {r[0] for r in live.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    raw = sorted(names & RAW_TRIGGERS.keys())
    print(f"raw-hypothesis triggers: {len(raw)}/12")
    for name in raw:
        print(f"  {name}")
    identity = IDENTITY_TRIGGER in names
    print(f"observation-identity trigger: {'present' if identity else 'MISSING'}")
    failures = [f"missing trigger {n}" for n in sorted(RAW_TRIGGERS.keys() - names)]
    if not identity:
        failures.append(f"missing trigger {IDENTITY_TRIGGER}")
    unexpected = sorted(names - RAW_TRIGGERS.keys() - {IDENTITY_TRIGGER})
    failures += [f"unexpected trigger {n}" for n in unexpected]
    print(f"unexpected triggers: {unexpected or 'none'}")
    return failures


def _rows(conn: sqlite3.Connection, table: str) -> dict:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    key = "id" if "id" in cols else "rowid"
    return {r[0]: dict(zip(cols, r[1:])) for r in conn.execute(
        f"SELECT {key}, {', '.join(cols)} FROM {table}")}


def check_rows_preserved(pre: sqlite3.Connection, live: sqlite3.Connection) -> list:
    failures = []
    tables = [r[0] for r in pre.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    pre_obs = set(_rows(pre, "classification_observations"))
    for table in tables:
        before, after = _rows(pre, table), _rows(live, table)
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
    return failures


def check_enforcement(work: sqlite3.Connection) -> list:
    failures = []
    obs = work.execute("SELECT id FROM classification_observations "
                       "ORDER BY created_at, id LIMIT 1").fetchone()
    if obs is None:
        return ["no observation to test enforcement on"]
    targets = [(c, f"IMMUTABLE_RAW_HYPOTHESIS: classification_observations.{c} "
                   "cannot be changed after insert") for c in RAW_COLUMNS]
    targets.append(("id", "IMMUTABLE_OBSERVATION_IDENTITY: "
                          "classification_observations.id cannot be changed after insert"))
    refused = 0
    for column, message in targets:
        try:
            work.execute(f"UPDATE classification_observations SET {column} = ? "
                         "WHERE id = ?", (CHANGED.get(column, "d1_probe"), obs[0]))
            failures.append(f"UPDATE of {column} was NOT refused")
        except sqlite3.IntegrityError as exc:
            if str(exc) == message:
                refused += 1
            else:
                failures.append(f"{column}: unexpected message {exc}")
        work.rollback()
    print(f"  protected-column UPDATEs refused with exact message: {refused}/13")
    work.execute("UPDATE classification_observations SET review_reason = 'd1_probe' "
                 "WHERE id = ?", (obs[0],))
    work.rollback()
    print("  review-only UPDATE allowed: yes")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src-root", default="/app")
    parser.add_argument("--live-db", default="/data/core.db")
    parser.add_argument("--pre-db", default="/tmp/pre_d1.db")
    parser.add_argument("--work-db", default="/tmp/d1_verify.db")
    args = parser.parse_args()
    src_root, live_p = Path(args.src_root), Path(args.live_db)
    pre_p, work_p = Path(args.pre_db), Path(args.work_db)

    print("D1 VERIFY")
    for name in ("storage.py", "main.py", "core.py"):
        print(f"deployed {name:<11} {sha256(src_root / 'src' / name)}")
    print(f"pre_d1.db SHA256     {sha256(pre_p)}\n")

    live = sqlite3.connect(f"file:{live_p}?mode=ro", uri=True)
    pre = sqlite3.connect(f"file:{pre_p}?mode=ro", uri=True)
    failures = check_triggers(live)
    print("\nRows from pre_d1.db in the live database:")
    failures += check_rows_preserved(pre, live)
    pre.close()

    work_p.unlink(missing_ok=True)
    work = sqlite3.connect(work_p)
    live.backup(work)
    live.close()
    print("\nEnforcement (on a copy):")
    failures += check_enforcement(work)
    work.close()

    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'}")
    for failure in failures:
        print(f"  - {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
