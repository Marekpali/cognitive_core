"""Snapshot of the live database through the SQLite backup API.
Reads /data/core.db read-only; writes only the destination (default
/tmp/pre_d3.db, or the first argument). Counts and hash only."""
import hashlib
import sqlite3
import sys

SRC = "/data/core.db"
DST = sys.argv[1] if len(sys.argv) > 1 else "/tmp/pre_d3.db"
TABLES = ("classification_inputs", "classification_observations", "classification_sweeps",
          "precedent_annotations", "precedent_annotation_evidence", "precedent_audit")

live = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
copy = sqlite3.connect(DST)
live.backup(copy)
live.close()
existing = {r[0] for r in copy.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
for table in TABLES:
    if table in existing:
        print(table, copy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
print("review_cases", copy.execute(
    "SELECT status, COUNT(*) FROM review_cases GROUP BY status ORDER BY status").fetchall())
print("triggers", copy.execute(
    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0])
print("integrity", copy.execute("PRAGMA integrity_check").fetchone()[0])
copy.close()
print(DST, "SHA256", hashlib.sha256(open(DST, "rb").read()).hexdigest())
