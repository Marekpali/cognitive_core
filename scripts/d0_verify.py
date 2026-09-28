"""
D0 production verification: STEP 6.3 + 6.4 immutability on real data.

Run INSIDE the rebuilt container, after D0:

    docker exec app_local_cognitive_core python3 /tmp/d0_verify.py

Never writes to the live database:
  1. reads live state through a read-only connection (mode=ro);
  2. takes a consistent copy via the SQLite backup API into /tmp;
  3. runs the DEPLOYED code (/app/src) against that copy only: for every
     resolved case, a second resolve_review_case() and a legacy
     *_observation() call must return 'already_resolved';
  4. compares every decision field before/after, 1:1.

The copy is used because a write attempt against the live database is
exactly what this test must not risk - and a read-only connection cannot
be used for it (SQLite rejects any UPDATE on it, even one matching 0 rows).

Prints ids and hashes only, never device names. Exit 0 = PASS, 1 = FAIL.
"""

import argparse
import hashlib
import sqlite3
import sys
import warnings
from pathlib import Path

CASE_FIELDS = ("status, decision, corrected_category, decided_at, "
               "updated_at, last_observation_id")
OBS_FIELDS = ("id, review_status, human_decision, corrected_category, "
              "reviewed_at, review_reason, reviewed_by")
OTHER_DECISION = {"approved": "rejected", "rejected": "approved",
                  "corrected": "approved"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def counts(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM review_cases GROUP BY status").fetchall()
    return {status: n for status, n in rows}


def snapshot(conn: sqlite3.Connection) -> dict:
    """Every decision field of every resolved case and its labelled rows."""
    conn.row_factory = sqlite3.Row
    snap = {}
    for case in conn.execute(
            "SELECT id, device_id, classifier_name, hypothesis_category "
            "FROM review_cases WHERE status = 'resolved' ORDER BY id"):
        fields = conn.execute(
            f"SELECT {CASE_FIELDS} FROM review_cases WHERE id = ?",
            (case["id"],)).fetchone()
        labelled = conn.execute(
            f"SELECT {OBS_FIELDS} FROM classification_observations "
            "WHERE device_id = ? AND classifier_name = ? "
            "AND hypothesis_category = ? AND human_decision IS NOT NULL "
            "ORDER BY id",
            (case["device_id"], case["classifier_name"],
             case["hypothesis_category"])).fetchall()
        snap[case["id"]] = (dict(fields), [dict(r) for r in labelled])
    return snap


def attempt_overwrites(storage, snap: dict) -> list:
    """Try to overwrite every resolved decision; return failures."""
    failures = []
    for case_id, (case, labelled) in snap.items():
        second = OTHER_DECISION[case["decision"]]
        result = storage.resolve_review_case(
            case_id, expected_observation_id=case["last_observation_id"],
            decision=second)
        print(f"  resolve_review_case({case_id}, {second}) -> {result}")
        if result != "already_resolved":
            failures.append(f"{case_id}: resolve_review_case returned {result!r}")
        for row in labelled:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                legacy = storage.approve_observation(row["id"]) \
                    if row["human_decision"] != "approved" \
                    else storage.reject_observation(row["id"])
            print(f"  legacy decision on {row['id']} -> {legacy}")
            if legacy != "already_resolved":
                failures.append(f"{row['id']}: legacy method returned {legacy!r}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src-root", default="/app")
    parser.add_argument("--live-db", default="/data/core.db")
    parser.add_argument("--work-db", default="/tmp/d0_verify.db")
    args = parser.parse_args()

    src_root, live, work = Path(args.src_root), Path(args.live_db), Path(args.work_db)
    print("D0 VERIFY")
    for name in ("storage.py", "main.py", "core.py"):
        print(f"deployed {name:<11} {sha256(src_root / 'src' / name)}")

    live_conn = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    live_counts = counts(live_conn)
    print(f"\nlive review_cases: {live_counts}  (read-only)")

    work.unlink(missing_ok=True)
    work_conn = sqlite3.connect(work)
    live_conn.backup(work_conn)
    live_conn.close()
    before = snapshot(work_conn)
    work_conn.close()
    print(f"work copy: {work}  resolved cases snapshotted: {len(before)}\n")

    sys.path.insert(0, str(src_root))
    from src.storage import Storage  # the deployed code
    storage = Storage(work)
    print("Overwrite attempts (on the copy):")
    failures = attempt_overwrites(storage, before)
    storage.connection.close()

    after = snapshot(sqlite3.connect(work))
    if after != before:
        failures.append("decision fields changed between before/after snapshots")
    print(f"\n[{'PASS' if after == before else 'FAIL'}] all decision fields "
          f"identical before/after ({len(before)} cases)")
    if not before:
        failures.append("no resolved cases found - nothing was verified")

    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'}")
    for failure in failures:
        print(f"  - {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
