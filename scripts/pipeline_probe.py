"""
Read-only pipeline probe: counts that change when a device event is processed.

Run inside the container, before and after a controlled event:

    docker exec app_local_cognitive_core python3 /tmp/pipeline_probe.py

Opens /data/core.db with mode=ro (cannot write). Prints counts, the newest
observation and each review case's evidence pointer - ids and timestamps
only, no device names.
"""

import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "/data/core.db"

QUERIES = [
    ("classification_observations", "SELECT COUNT(*) FROM classification_observations"),
    ("assets", "SELECT COUNT(*) FROM assets"),
    ("review_cases pending", "SELECT COUNT(*) FROM review_cases WHERE status = 'pending'"),
    ("review_cases resolved", "SELECT COUNT(*) FROM review_cases WHERE status = 'resolved'"),
]

NEWEST_OBSERVATION = (
    "SELECT id, created_at FROM classification_observations "
    "ORDER BY created_at DESC, id DESC LIMIT 1"
)


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        for name, sql in QUERIES:
            print(f"{name}: {conn.execute(sql).fetchone()[0]}")
        newest = conn.execute(NEWEST_OBSERVATION).fetchone()
        print("newest observation: "
              + (f"{newest[0]} @ {newest[1]}" if newest else "none"))
        print("review_cases (id, status, decision, last_observation_id, updated_at):")
        for row in conn.execute(
                "SELECT id, status, decision, last_observation_id, updated_at "
                "FROM review_cases ORDER BY id"):
            print("  " + " | ".join(str(v) for v in row))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
