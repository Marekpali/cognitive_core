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

queries = [
    ("classification_observations", "SELECT COUNT(*) FROM classification_observations"),
    ("review_cases", "SELECT COUNT(*) FROM review_cases"),
    ("pending_review_cases", "SELECT COUNT(*) FROM review_cases WHERE status='pending'"),
    ("resolved_review_cases", "SELECT COUNT(*) FROM review_cases WHERE status='resolved'"),
]

for name, sql in queries:
    count = conn.execute(sql).fetchone()[0]
    print(name + ":", count)

conn.close()
