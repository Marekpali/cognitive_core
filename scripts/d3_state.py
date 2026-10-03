"""Read-only state of the live database after D3. Counts only."""
import sqlite3

live = sqlite3.connect("file:/data/core.db?mode=ro", uri=True)
for table in ("precedent_annotations", "precedent_annotation_evidence", "precedent_audit",
              "classification_inputs", "classification_observations", "classification_sweeps"):
    print(table, live.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
print("review_cases", live.execute(
    "SELECT status, COUNT(*) FROM review_cases GROUP BY status ORDER BY status").fetchall())
print("labelled observations", live.execute(
    "SELECT COUNT(*) FROM classification_observations WHERE human_decision IS NOT NULL").fetchone()[0])
print("triggers", live.execute(
    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0])
print("last sweep", live.execute(
    "SELECT id, mode, source, unchanged, classified, no_match, error "
    "FROM classification_sweeps ORDER BY rowid DESC LIMIT 1").fetchone())
