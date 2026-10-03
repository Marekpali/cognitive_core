"""Snapshot of the live database through the SQLite backup API.
Reads /data/core.db read-only; writes only /tmp/pre_d3.db. Counts and hash only."""
import hashlib
import sqlite3

SRC, DST = "/data/core.db", "/tmp/pre_d3.db"
live = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
copy = sqlite3.connect(DST)
live.backup(copy)
live.close()
for table in ("classification_inputs", "classification_observations", "classification_sweeps"):
    print(table, copy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
print("review_cases", copy.execute(
    "SELECT status, COUNT(*) FROM review_cases GROUP BY status ORDER BY status").fetchall())
print("integrity", copy.execute("PRAGMA integrity_check").fetchone()[0])
copy.close()
print("SHA256", hashlib.sha256(open(DST, "rb").read()).hexdigest())
