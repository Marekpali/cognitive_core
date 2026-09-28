"""
M0 pre-check for a COPY of the production database (STEP 6.3 / 6.4 / 7).

Usage (on Windows, against a copied file - never the live /data/core.db):

    python scripts/m0_precheck.py path/to/core_m0.db

Opens the database strictly read-only (SQLite URI mode=ro) and reports the
database SHA256 before and after, proving the copy was not modified, plus
the SHA256, git commit and git blob id of this script itself, so the
result is traceable to the exact tool version that produced it.

Output contains counts and record identifiers only - never device names or
full records. Identifiers are printed only for checks that fail.

Interpretation: a clean result means no detectable inconsistencies or
overwrites remain in the current database state. It does NOT prove that
historical overwrites never occurred: before STEP 6.3, resolve_review_case()
could re-resolve a case, updating review_cases and the labelled observation
consistently and leaving no reconstructable trace. M0 is a trusted baseline
from this point forward, not a proof of historical immutability.

Exit code 0 = all blocking checks passed, 1 = at least one failed,
2 = usage error.
"""

import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path

MAX_IDS_SHOWN = 20

# (name, SQL returning offending ids). Every row returned is an inconsistency.
BLOCKING_CHECKS = [
    ("logical keys with more than one labelled observation "
     "(get_correction_patterns precondition)",
     """SELECT device_id || ' | ' || classifier_name || ' | ' || hypothesis_category
        FROM classification_observations
        WHERE human_decision IS NOT NULL
        GROUP BY device_id, classifier_name, hypothesis_category
        HAVING COUNT(*) > 1"""),
    ("pending cases whose evidence row is already labelled "
     "(STEP 6.3 guard would refuse to resolve them)",
     """SELECT rc.id FROM review_cases rc
        JOIN classification_observations co ON co.id = rc.last_observation_id
        WHERE rc.status = 'pending' AND co.human_decision IS NOT NULL"""),
    ("resolved cases with no labelled observation matching their decision "
     "(decision history diverged)",
     """SELECT rc.id FROM review_cases rc
        WHERE rc.status = 'resolved' AND NOT EXISTS (
            SELECT 1 FROM classification_observations co
            WHERE co.device_id = rc.device_id
              AND co.classifier_name = rc.classifier_name
              AND co.hypothesis_category = rc.hypothesis_category
              AND co.human_decision = rc.decision
              AND co.corrected_category IS rc.corrected_category)"""),
    ("review cases pointing at a missing observation (FKs are not enforced)",
     """SELECT rc.id FROM review_cases rc
        LEFT JOIN classification_observations co ON co.id = rc.last_observation_id
        WHERE co.id IS NULL"""),
]

COUNTS = [
    ("classification_observations rows",
     "SELECT COUNT(*) FROM classification_observations"),
    ("observations carrying a human decision",
     "SELECT COUNT(*) FROM classification_observations WHERE human_decision IS NOT NULL"),
    ("review_cases pending",
     "SELECT COUNT(*) FROM review_cases WHERE status = 'pending'"),
    ("review_cases resolved",
     "SELECT COUNT(*) FROM review_cases WHERE status = 'resolved'"),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(args: list, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


def script_git_identity() -> tuple:
    """(commit, blob) for this script. The git blob id is line-ending
    independent (git normalises CRLF/LF), unlike the file SHA256, so it is
    the stable identifier of the tool version."""
    here = Path(__file__).resolve()
    try:
        commit = _git(["log", "-1", "--format=%H", "--", here.name], here.parent)
        dirty = _git(["status", "--porcelain", "--", here.name], here.parent)
        blob = _git(["hash-object", here.name], here.parent)
    except (OSError, subprocess.CalledProcessError):
        return "unavailable (not run from a git checkout)", "unavailable"
    if not commit:
        return "untracked (not committed)", blob
    return commit + (" + UNCOMMITTED CHANGES" if dirty else ""), blob


def print_header(path: Path, db_hash: str) -> None:
    script = Path(__file__).resolve()
    print("M0 PRECHECK")
    print(f"script SHA256:    {sha256(script)}")
    commit, blob = script_git_identity()
    print(f"script commit:    {commit}")
    print(f"script git blob:  {blob}")
    print(f"database file:    {path.name}")
    print(f"database size:    {path.stat().st_size} bytes")
    print(f"database SHA256:  {db_hash}\n")


def run_blocking_checks(conn: sqlite3.Connection) -> int:
    failed = 0
    integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    ok = integrity == ["ok"]
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] PRAGMA integrity_check: {'; '.join(integrity)}")

    for name, sql in BLOCKING_CHECKS:
        ids = [row[0] for row in conn.execute(sql)]
        if not ids:
            print(f"[PASS] {name}: 0")
            continue
        failed += 1
        print(f"[FAIL] {name}: {len(ids)}")
        for record_id in ids[:MAX_IDS_SHOWN]:
            print(f"         - {record_id}")
        if len(ids) > MAX_IDS_SHOWN:
            print(f"         ... and {len(ids) - MAX_IDS_SHOWN} more")
    return failed


def print_inventory(conn: sqlite3.Connection) -> None:
    print()
    for name, sql in COUNTS:
        print(f"[INFO] {name}: {conn.execute(sql).fetchone()[0]}")
    triggers = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name")]
    print(f"[INFO] SQLite triggers: {len(triggers)}"
          + (f" ({', '.join(triggers)})" if triggers else ""))


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"ERROR: {path} not found")
        return 2

    before = sha256(path)
    print_header(path, before)

    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        failed = run_blocking_checks(conn)
        print_inventory(conn)
    finally:
        conn.close()

    unchanged = sha256(path) == before
    failed += not unchanged
    print(f"\n[{'PASS' if unchanged else 'FAIL'}] database SHA256 unchanged by this check")

    print(f"\nRESULT: {'CLEAN' if not failed else f'{failed} CHECK(S) FAILED'}")
    if not failed:
        print("Meaning: no detectable inconsistencies or overwrites remain in the "
              "current database state.\nNOT a proof that historical overwrites "
              "never occurred (pre-STEP 6.3 re-resolution left no trace).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
