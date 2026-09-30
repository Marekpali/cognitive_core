"""STEP 7P storage: classifier inputs, sweep coverage records, and their
database-level protection. See docs/STEP7_OBSERVATION_SOURCES.md (6-8, 11).

- classification_inputs: one row per device whose fingerprint changed and
  whose classification succeeded (outcome classified or no_match).
  Append-only.
- classification_sweeps: one row per sweep, shadow or active, with counts
  and the per-device outcome/fingerprint map (devices_json). Append-only.
- classification_observations.input_id: links an observation to the input
  it was computed from; protected like the D1 raw-hypothesis columns.

The D1 triggers are not touched; everything here uses new trigger names.
"""

import json
import sqlite3
from typing import Optional

INPUT_ID_TRIGGER = "trg_obs_immutable_input_id"
APPEND_ONLY_TABLES = ("classification_inputs", "classification_sweeps")
STEP7P_TRIGGERS = (INPUT_ID_TRIGGER,) + tuple(
    f"trg_{table}_no_{op}" for table in APPEND_ONLY_TABLES for op in ("update", "delete")
)

# Outcomes after which the device's fingerprint counts as the gate baseline.
GATE_ADVANCING_OUTCOMES = frozenset({"classified", "no_match", "unchanged"})
SWEEP_COUNT_FIELDS = (
    "discovered", "skipped_no_entities", "skipped_missing_metadata",
    "unchanged", "classified", "no_match", "error",
    "excluded_disabled_entities", "unavailable_entities",
)


def ensure_schema(cursor: sqlite3.Cursor) -> None:
    """Idempotent. Must run after classification_observations exists."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS classification_inputs (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            classifier_set_version TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('classified', 'no_match')),
            matched_classifiers TEXT NOT NULL,
            sweep_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_inputs_device
        ON classification_inputs(device_id)
    """)
    counts = ",\n".join(f"{name} INTEGER NOT NULL" for name in SWEEP_COUNT_FIELDS)
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS classification_sweeps (
            id TEXT PRIMARY KEY,
            mode TEXT NOT NULL CHECK (mode IN ('shadow', 'active')),
            source TEXT NOT NULL,
            classifier_set_version TEXT NOT NULL,
            baseline_sweep_id TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            {counts},
            devices_json TEXT NOT NULL
        )
    """)

    columns = {row[1] for row in cursor.execute(
        "PRAGMA table_info(classification_observations)")}
    if "input_id" not in columns:
        # Existing rows get NULL: no input was recorded for them, and the
        # trigger below prevents backfilling one afterwards.
        cursor.execute("ALTER TABLE classification_observations ADD COLUMN input_id TEXT")

    cursor.execute(f"""
        CREATE TRIGGER IF NOT EXISTS {INPUT_ID_TRIGGER}
        BEFORE UPDATE OF input_id ON classification_observations
        WHEN OLD.input_id IS NOT NEW.input_id
        BEGIN
            SELECT RAISE(ABORT, 'IMMUTABLE_OBSERVATION_INPUT: classification_observations.input_id cannot be changed after insert');
        END
    """)
    for table in APPEND_ONLY_TABLES:
        for op in ("update", "delete"):
            cursor.execute(f"""
                CREATE TRIGGER IF NOT EXISTS trg_{table}_no_{op}
                BEFORE {op.upper()} ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'APPEND_ONLY: {table} rows cannot be {op}d');
                END
            """)
    print(f"[STORAGE] STEP 7P schema verified ({len(STEP7P_TRIGGERS)} triggers)")


def latest_input_fingerprints(conn: sqlite3.Connection) -> dict:
    """Active-mode gate baseline: device_id -> fingerprint of its latest input."""
    rows = conn.execute("""
        SELECT device_id, fingerprint FROM classification_inputs
        WHERE rowid IN (SELECT MAX(rowid) FROM classification_inputs GROUP BY device_id)
    """).fetchall()
    return {row[0]: row[1] for row in rows}


def latest_shadow_baseline(conn: sqlite3.Connection) -> tuple:
    """Shadow-mode gate baseline: (sweep_id, {device_id: fingerprint}).

    Taken from the most recent shadow sweep. Only devices whose outcome
    there advanced the gate count; skipped/error devices are re-attempted.
    """
    row = conn.execute("""
        SELECT id, devices_json FROM classification_sweeps
        WHERE mode = 'shadow' ORDER BY rowid DESC LIMIT 1
    """).fetchone()
    if row is None:
        return None, {}
    devices = json.loads(row[1])
    return row[0], {
        device_id: entry["fingerprint"]
        for device_id, entry in devices.items()
        if entry.get("outcome") in GATE_ADVANCING_OUTCOMES and entry.get("fingerprint")
    }


def insert_input(cursor: sqlite3.Cursor, row: dict) -> None:
    cursor.execute("""
        INSERT INTO classification_inputs (
            id, device_id, fingerprint, classifier_set_version, snapshot_json,
            outcome, matched_classifiers, sweep_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (row["id"], row["device_id"], row["fingerprint"],
          row["classifier_set_version"], row["snapshot_json"], row["outcome"],
          json.dumps(row["matched_classifiers"]), row["sweep_id"], row["created_at"]))


def insert_sweep(conn: sqlite3.Connection, row: dict) -> None:
    fields = ("id", "mode", "source", "classifier_set_version", "baseline_sweep_id",
              "started_at", "finished_at") + SWEEP_COUNT_FIELDS + ("devices_json",)
    values = [row[f] for f in fields[:-1]] + [
        json.dumps(row["devices"], sort_keys=True, ensure_ascii=False)]
    conn.execute(
        f"INSERT INTO classification_sweeps ({', '.join(fields)}) "
        f"VALUES ({', '.join('?' for _ in fields)})", values)
    conn.commit()


def get_input(conn: sqlite3.Connection, input_id: str) -> Optional[dict]:
    row = conn.execute("""
        SELECT id, device_id, fingerprint, classifier_set_version, snapshot_json,
               outcome, matched_classifiers, sweep_id, created_at
        FROM classification_inputs WHERE id = ?
    """, (input_id,)).fetchone()
    if row is None:
        return None
    keys = ("id", "device_id", "fingerprint", "classifier_set_version",
            "snapshot_json", "outcome", "matched_classifiers", "sweep_id", "created_at")
    record = dict(zip(keys, row))
    record["snapshot"] = json.loads(record.pop("snapshot_json"))
    record["matched_classifiers"] = json.loads(record["matched_classifiers"])
    return record
