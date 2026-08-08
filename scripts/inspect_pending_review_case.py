"""
HAOS Cognitive Core inspection utility.

Run inside the Cognitive Core container, for example:

    docker exec -it app_local_cognitive_core python3 /path/to/script.py

This script reads the runtime database:
    /data/core.db

It is intended for production inspection only.
It does not modify data.
"""

import sqlite3

conn = sqlite3.connect('/data/core.db')
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT
        rc.id AS case_id,
        rc.device_id,
        co.device_name,
        rc.classifier_name,
        rc.hypothesis_category,
        co.hypothesis_confidence,
        co.id AS observation_id,
        rc.status,
        rc.decision,
        rc.corrected_category,
        rc.created_at AS case_created_at,
        rc.updated_at AS case_updated_at
    FROM review_cases rc
    JOIN classification_observations co
      ON co.id = rc.last_observation_id
    WHERE rc.status = 'pending'
    ORDER BY rc.updated_at DESC
""").fetchall()

if not rows:
    print("No pending review cases")
else:
    print(f"Pending review cases: {len(rows)}")
    for i, row in enumerate(rows, 1):
        print(f"\n--- Case {i} ---")
        print("case_id:", row["case_id"])
        print("device_id:", row["device_id"])
        print("device_name:", row["device_name"])
        print("classifier_name:", row["classifier_name"])
        print("hypothesis_category:", row["hypothesis_category"])
        print("hypothesis_confidence:", row["hypothesis_confidence"])
        print("observation_id:", row["observation_id"])
        print("status:", row["status"])
        print("decision:", row["decision"])
        print("corrected_category:", row["corrected_category"])
        print("case_created_at:", row["case_created_at"])
        print("case_updated_at:", row["case_updated_at"])

conn.close()
