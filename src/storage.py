# src/storage.py

import sqlite3
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Optional, List
import json
import warnings

from src import coverage_store


# M1 (docs/STEP7_ARCHITECTURE.md): the raw classifier observation is
# immutable once inserted. One trigger per column so the abort message can
# name the exact column (SQLite RAISE() only accepts a literal message).
# Review/workflow columns (review_status, human_decision, corrected_category,
# reviewed_at, review_reason, reviewed_by) are deliberately NOT listed.
RAW_HYPOTHESIS_COLUMNS = (
    "observation_group_id",
    "device_id",
    "classifier_name",
    "hypothesis_category",
    "hypothesis_confidence",
    "hypothesis_reasoning",
    "device_name",
    "device_model",
    "device_manufacturer",
    "device_source_adapter",
    "device_entity_count",
    "created_at",
)


# M1: record identity is protected separately from the raw hypothesis -
# `id` is not classifier output. Model: record identity immutable, raw
# hypothesis immutable, review metadata mutable.
OBSERVATION_IDENTITY_TRIGGER = "trg_obs_immutable_id"

class DecisionResult(str):
    """Outcome of a legacy observation-level decision (STEP 6.4).

    Compares like the plain strings resolve_review_case() returns
    ('resolved', 'already_resolved', 'not_found'), but is truthy ONLY for
    'resolved'. The legacy methods used to return a bool; a non-empty
    string such as 'already_resolved' would otherwise be truthy, and an
    old `if storage.approve_observation(...):` caller would mistake a
    refused write for success.

    Compatibility boundary: backward compatible with callers that use
    truth-value testing (`if approve_observation(...):`, `not result`,
    `bool(result)`). NOT compatible with callers that compare the return
    value directly to a bool (`approve_observation(...) == True` is now
    always False) - such callers must migrate to truth-value testing or
    to comparing against 'resolved'. Equality with True/False is
    deliberately not special-cased; that would make the type less
    predictable.
    """

    def __bool__(self) -> bool:
        return self == "resolved"

class Storage:
    """SQLite-based operational data storage"""

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize storage, resolving the database path.

        STEP 4A.1b: db_path resolution order:
        1. Explicit db_path argument (used by CognitiveCore, which
           passes it from DB_PATH env or a default, and by tests,
           which pass a temp path)
        2. DB_PATH environment variable (used when Storage() is
           constructed with no argument, e.g. from a `docker exec`
           CLI invocation - this does NOT inherit variables exported
           by run.sh in the main process, only what the container
           environment itself defines)
        3. Hardcoded '/data/core.db' fallback - the add-on's
           persistent storage path, so a bare `Storage()` call always
           resolves to the same production database regardless of
           the caller's working directory.

        Before this fix, the default was a relative Path("data/core.db"),
        which silently resolved differently depending on cwd - matching
        the running add-on process (cwd=/app) by coincidence, but
        creating a separate, ephemeral database under `docker exec`
        (also cwd=/app, but /app/data/core.db is not the same file as
        the mounted /data/core.db).
        """
        self.db_path = (
            Path(db_path)
            if db_path is not None
            else Path(os.getenv("DB_PATH", "/data/core.db"))
        )
        if not self.db_path.parent.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = None
        self.init_schema()

    def init_schema(self):
        """Create database schema"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Assets table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_device_id TEXT,
                source_adapter TEXT,

                hypothesis_category TEXT NOT NULL,
                hypothesis_confidence REAL NOT NULL,
                hypothesis_reasoning TEXT,
                hypothesis_classifier TEXT,

                lifecycle_state TEXT NOT NULL DEFAULT 'provisional',
                lifecycle_discovered_at TEXT NOT NULL,

                device_data JSON NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')

        # Legacy review queue table. Its runtime read/write path was retired
        # after review_cases became the only active Human Review workflow.
        # Keep the inert schema for one release cycle so historical rows are
        # preserved until a dedicated migration removes the table.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS review_queue (
                id TEXT PRIMARY KEY,
                asset_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                decision TEXT,
                created_at TEXT NOT NULL,
                decided_at TEXT,

                FOREIGN KEY(asset_id) REFERENCES assets(id)
            )
        ''')

        # Environmental readings table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS environmental_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id TEXT NOT NULL,
                timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                temperature REAL,
                humidity REAL,
                illuminance REAL,
                motion_detected INTEGER,
                battery_percent REAL,

                created_at TEXT NOT NULL,

                FOREIGN KEY(asset_id) REFERENCES assets(id)
            )
        ''')

        # Indexes
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_assets_category '
            'ON assets(hypothesis_category)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_review_status '
            'ON review_queue(status)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_env_readings_asset_time '
            'ON environmental_readings(asset_id, timestamp DESC)'
        )

        # NEW: Classification observations table (STEP 1)
        self.init_classification_observations_schema(cursor)

        # M1: raw hypothesis immutability triggers (idempotent)
        self._ensure_raw_hypothesis_immutability(cursor)

        # M1: observation identity immutability trigger (idempotent)
        self._ensure_observation_identity_immutability(cursor)

        # STEP 7P: classifier inputs, sweep records, input_id link
        self.init_step7p_schema(cursor)

        # STEP 3: Ensure review columns exist (idempotent migration)
        self._ensure_review_columns(conn)

        # STEP 5: Review cases table (materialized Human Review workflow
        # state, separate from append-only classification_observations
        # history). Must be created after classification_observations
        # (FK dependency) and after review columns exist (backfill below
        # reads review_status/human_decision).
        self.init_review_cases_schema(cursor)

        # Commit ALL changes in one transaction
        conn.commit()
        conn.close()

        # STEP 5: backfill/reconcile review_cases from history. Runs
        # AFTER the schema transaction above is committed and closed -
        # it opens its own connection via self.connect(), and an
        # uncommitted CREATE TABLE on the local `conn` above would not
        # yet be visible to a second connection to the same database
        # file.
        self.backfill_review_cases()

    def init_classification_observations_schema(self, cursor):
        """Create classification_observations table.

        STEP 1 MINIMAL SCHEMA:
        - Basic observation logging (no deduplication yet)
        - No is_duplicate, duplicate_of_id, or conflict detection
        - Simple indices for device and group queries

        Called from init_schema() during system startup.
        Uses cursor from init_schema() - commit happens in init_schema().
        """

        print("[STORAGE] Initializing classification_observations schema...")

        # TABLE: classification_observations (MINIMAL)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS classification_observations (
                id TEXT PRIMARY KEY,
                observation_group_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                classifier_name TEXT NOT NULL,
                hypothesis_category TEXT NOT NULL,
                hypothesis_confidence REAL NOT NULL,
                hypothesis_reasoning TEXT,
                device_name TEXT,
                device_model TEXT,
                device_manufacturer TEXT,
                device_source_adapter TEXT NOT NULL,
                device_entity_count INTEGER,
                created_at TEXT NOT NULL
            )
        ''')

        print("[STORAGE] Table classification_observations created/verified")

        # INDICES
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_obs_device_time
            ON classification_observations(device_id, created_at DESC)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_obs_group_id
            ON classification_observations(observation_group_id)
        ''')

        print("[STORAGE] Indices created/verified (2 indices)")
        print("[STORAGE] classification_observations schema initialization complete - OK")

    def _ensure_raw_hypothesis_immutability(self, cursor) -> None:
        """M1: abort any UPDATE that CHANGES a raw classifier field.

        Value-change semantics: `WHEN OLD.col IS NOT NEW.col` (NULL-safe),
        so re-assigning the same value (`SET col = col`) is allowed and only
        a real change is refused. Creating the triggers reads and writes no
        row data. CREATE TRIGGER IF NOT EXISTS makes this idempotent; a
        future change to the trigger body needs a new trigger name.

        Not covered by design: DELETE of observation rows, and INSERT
        (new observations are the normal write path).
        """
        for column in RAW_HYPOTHESIS_COLUMNS:
            cursor.execute(f'''
                CREATE TRIGGER IF NOT EXISTS trg_obs_immutable_{column}
                BEFORE UPDATE OF {column} ON classification_observations
                WHEN OLD.{column} IS NOT NEW.{column}
                BEGIN
                    SELECT RAISE(ABORT, 'IMMUTABLE_RAW_HYPOTHESIS: classification_observations.{column} cannot be changed after insert');
                END
            ''')
        print(f"[STORAGE] Raw hypothesis immutability triggers verified "
              f"({len(RAW_HYPOTHESIS_COLUMNS)} columns)")

    def _ensure_observation_identity_immutability(self, cursor) -> None:
        """M1: abort any UPDATE that changes classification_observations.id.

        The id is referenced by review_cases.last_observation_id (and, from
        STEP 7a, by precedent annotations); changing it would orphan those
        references. Same value-change semantics as the raw hypothesis
        triggers: `SET id = id` is allowed.
        """
        cursor.execute(f'''
            CREATE TRIGGER IF NOT EXISTS {OBSERVATION_IDENTITY_TRIGGER}
            BEFORE UPDATE OF id ON classification_observations
            WHEN OLD.id IS NOT NEW.id
            BEGIN
                SELECT RAISE(ABORT, 'IMMUTABLE_OBSERVATION_IDENTITY: classification_observations.id cannot be changed after insert');
            END
        ''')
        print("[STORAGE] Observation identity immutability trigger verified")

    def init_step7p_schema(self, cursor) -> None:
        """STEP 7P tables, input_id column and their triggers (idempotent).

        Separate method so tests can reconstruct a D1-era database.
        """
        coverage_store.ensure_schema(cursor)

    def init_review_cases_schema(self, cursor):
        """Create review_cases table.

        STEP 5: Materialized Human Review workflow state, separate from the
        append-only classification_observations history.

        Logical review identity key: (device_id, classifier_name,
        hypothesis_category). Confidence is deliberately NOT part of the
        key - it is an attribute of a hypothesis, not its identity
        (corrections only ever touch hypothesis_category, never
        confidence).

        Called from init_schema() during system startup. Uses cursor from
        init_schema() - commit happens in init_schema().
        """
        print("[STORAGE] Initializing review_cases schema...")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS review_cases (
                id                   TEXT PRIMARY KEY,
                device_id            TEXT NOT NULL,
                classifier_name      TEXT NOT NULL,
                hypothesis_category  TEXT NOT NULL,

                status               TEXT NOT NULL DEFAULT 'pending',
                decision             TEXT,
                corrected_category   TEXT,

                last_observation_id  TEXT NOT NULL,
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL,
                decided_at           TEXT,

                UNIQUE(device_id, classifier_name, hypothesis_category),
                FOREIGN KEY(last_observation_id)
                    REFERENCES classification_observations(id)
            )
        ''')

        print("[STORAGE] Table review_cases created/verified")

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_review_cases_status
            ON review_cases(status)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_review_cases_device
            ON review_cases(device_id)
        ''')

        print("[STORAGE] Indices created/verified (2 indices)")
        print("[STORAGE] review_cases schema initialization complete - OK")

    def _ensure_review_columns(self, conn) -> None:
        """STEP 3: Idempotent migration - add review columns if missing.

        Checks which columns exist, adds only the missing ones.
        Safe to call on every startup - running twice does nothing on
        the second run.

        Wrapped in try/except with rollback: if the process is killed
        or crashes mid-migration, the failed attempt does not leave a
        committed partial state. Because this method is idempotent,
        a retry on the next startup picks up cleanly regardless.

        Columns added to classification_observations:
        - review_status: 'pending' | 'reviewed' (default 'pending')
        - human_decision: 'approved' | 'rejected' | 'corrected' | NULL
        - reviewed_at: ISO timestamp when reviewed, NULL if pending
        - review_reason: Optional text notes from reviewer
        - corrected_category: STEP 3C - the human-supplied correct
          category when human_decision='corrected'. Kept separate from
          hypothesis_category so the classifier's original (wrong)
          guess is preserved for accuracy tracking.
        - reviewed_by: STEP 3C - who made the decision. Defaults to
          'human' today; leaves room for named reviewers or automated
          review agents later without a schema change.
        """
        print("[STORAGE] Checking for review columns...")

        cursor = conn.cursor()

        existing = {
            row[1]
            for row in cursor.execute(
                "PRAGMA table_info(classification_observations)"
            ).fetchall()
        }

        columns = {
            "review_status": "TEXT DEFAULT 'pending'",
            "human_decision": "TEXT",
            "reviewed_at": "TEXT",
            "review_reason": "TEXT",
            "corrected_category": "TEXT",
            "reviewed_by": "TEXT",
        }

        to_add = {
            name: definition
            for name, definition in columns.items()
            if name not in existing
        }

        if not to_add:
            print("[STORAGE] All review columns already present")
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_classification_observations_review_status
                ON classification_observations(review_status)
            """)
            conn.commit()
            print("[STORAGE] Index verified: idx_classification_observations_review_status")
            return

        try:
            for name, definition in to_add.items():
                cursor.execute(
                    f"ALTER TABLE classification_observations "
                    f"ADD COLUMN {name} {definition}"
                )
                print(f"[STORAGE] Added column: {name}")

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_classification_observations_review_status
                ON classification_observations(review_status)
            """)

            conn.commit()
            print(f"[STORAGE] Migrated {len(to_add)} column(s)")
            print("[STORAGE] Index verified: idx_classification_observations_review_status")

        except Exception as exc:
            conn.rollback()
            print(f"[STORAGE] ERROR: migration failed, rolled back: {exc}")
            raise

    def connect(self):
        """Get database connection"""
        if not self.connection:
            self.connection = sqlite3.connect(self.db_path)
            self.connection.row_factory = sqlite3.Row
        return self.connection

    def save_asset(self, asset: Dict) -> str:
        """Save asset"""
        conn = self.connect()
        cursor = conn.cursor()

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        cursor.execute('''
            INSERT OR REPLACE INTO assets (
                id, name, source_device_id, source_adapter,
                hypothesis_category, hypothesis_confidence,
                hypothesis_reasoning, hypothesis_classifier,
                lifecycle_state, lifecycle_discovered_at,
                device_data, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            asset['id'],
            asset['name'],
            asset.get('source_device_id'),
            asset.get('source_adapter'),
            asset['hypothesis']['category'],
            asset['hypothesis']['confidence'],
            asset['hypothesis'].get('reasoning', ''),
            asset['hypothesis'].get('classifier', ''),
            asset.get('lifecycle_state', 'provisional'),
            asset['lifecycle_discovered_at'],
            json.dumps(asset),
            now,
            now
        ))

        conn.commit()
        print(f"[STORAGE] Saved asset: {asset['id']}")
        return asset['id']

    def load_asset(self, asset_id: str) -> Optional[Dict]:
        """Load asset"""
        conn = self.connect()
        cursor = conn.cursor()

        cursor.execute('SELECT device_data FROM assets WHERE id = ?', (asset_id,))
        row = cursor.fetchone()

        if row:
            return json.loads(row['device_data'])
        return None

    def log_classification_observation(self, payload: Dict) -> Optional[str]:
        """Log a single classifier observation.

        STEP 2: Classification Observations Logging

        Args:
            payload: {
                "observation_group_id": str (correlation ID for one device pass),
                "device_id": str,
                "classifier_name": str,
                "hypothesis_category": str,
                "hypothesis_confidence": float (0.0-1.0, not 0-100),
                "hypothesis_reasoning": str,
                "device_name": str,
                "device_model": str,
                "device_manufacturer": str,
                "device_source_adapter": str (e.g., "ha"),
                "device_entity_count": int,
            }

        Returns:
            observation_id (str) or None if logging failed

        Notes:
            - Uses own ID generation (obs_xxxxx), not lastrowid
            - Follows same pattern as insert_environmental_reading()
            - Errors are logged but don't stop asset creation
            - Confidence is stored as-is (0.0-1.0), displayed as percentage
        """
        conn = self.connect()
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        try:
            observation_id = self._insert_observation(cursor, payload, now)
            conn.commit()
            print(
                f"[STORAGE] Observation logged: {observation_id} "
                f"({payload['classifier_name']} @ "
                f"{payload['hypothesis_confidence'] * 100:.0f}%)"
            )
            return observation_id

        except Exception as exc:
            conn.rollback()
            print(f"[STORAGE] WARNING: observation logging failed: {exc}")
            return None

    @staticmethod
    def _insert_observation(cursor, payload: Dict, now: str) -> str:
        """INSERT one observation row without committing; returns its id.

        input_id (STEP 7P) is written only when the payload carries one;
        otherwise the column keeps its NULL default, exactly as before 7P.
        """
        from uuid import uuid4

        observation_id = f"obs_{uuid4().hex[:12]}"
        row = {
            "id": observation_id,
            **{key: payload[key] for key in (
                "observation_group_id", "device_id", "classifier_name",
                "hypothesis_category", "hypothesis_confidence",
                "hypothesis_reasoning", "device_name", "device_model",
                "device_manufacturer", "device_source_adapter",
                "device_entity_count")},
            "created_at": now,
        }
        if payload.get("input_id") is not None:
            row["input_id"] = payload["input_id"]
        cursor.execute(
            f"INSERT INTO classification_observations ({', '.join(row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )
        return observation_id

    def record_classification_input(self, input_row: Dict,
                                    observations: List[Dict]) -> List[str]:
        """STEP 7P: write one input and its observations atomically.

        Either the input and every observation are committed, or nothing is
        (so the fingerprint gate can never advance without its
        observations). Each observation payload gets input_id set to the
        input's id. Returns the observation ids. Raises on failure.
        """
        conn = self.connect()
        cursor = conn.cursor()
        try:
            coverage_store.insert_input(cursor, input_row)
            ids = [
                self._insert_observation(
                    cursor, dict(obs, input_id=input_row["id"]), input_row["created_at"])
                for obs in observations
            ]
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return ids

    def upsert_review_case(
        self,
        device_id: str,
        classifier_name: str,
        hypothesis_category: str,
        observation_id: str,
    ) -> str:
        """Atomically create or refresh the logical review case.

        STEP 5: single INSERT ... ON CONFLICT DO UPDATE ... WHERE, not a
        SELECT-then-branch - closes the race window if two classification
        callbacks for the same logical key ran concurrently.

        The DO UPDATE is conditional (monotonic): last_observation_id
        only advances if the incoming observation is actually newer than
        the case's current evidence pointer, using the same deterministic
        freshness rule as backfill_review_cases() - created_at DESC, id
        DESC as tie-breaker. Without this guard, two concurrent upserts
        for an older (O1) and a newer (O2) observation landing in the
        "wrong" commit order could regress last_observation_id from O2
        back to O1. If the incoming observation is not newer, SQLite's
        UPSERT WHERE clause makes the UPDATE a no-op - the statement
        still succeeds, nothing is written, no error is raised.

        status, decision, corrected_category, decided_at are never part
        of the DO UPDATE clause - a resolved case never reopens just
        because the same hypothesis was observed again.
        """
        from uuid import uuid4

        conn = self.connect()
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        case_id = f"case_{uuid4().hex[:8]}"

        cursor.execute('''
            INSERT INTO review_cases (
                id, device_id, classifier_name, hypothesis_category,
                status, last_observation_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
            ON CONFLICT(device_id, classifier_name, hypothesis_category)
            DO UPDATE SET
                last_observation_id = excluded.last_observation_id,
                updated_at = excluded.updated_at
            WHERE
                (SELECT created_at FROM classification_observations
                 WHERE id = excluded.last_observation_id)
                >
                (SELECT created_at FROM classification_observations
                 WHERE id = review_cases.last_observation_id)
                OR (
                    (SELECT created_at FROM classification_observations
                     WHERE id = excluded.last_observation_id)
                    =
                    (SELECT created_at FROM classification_observations
                     WHERE id = review_cases.last_observation_id)
                    AND excluded.last_observation_id > review_cases.last_observation_id
                )
        ''', (case_id, device_id, classifier_name, hypothesis_category,
              observation_id, now, now))
        conn.commit()

        cursor.execute('''
            SELECT id FROM review_cases
            WHERE device_id = ? AND classifier_name = ? AND hypothesis_category = ?
        ''', (device_id, classifier_name, hypothesis_category))
        real_case_id = cursor.fetchone()['id']
        print(f"[STORAGE] Review case upserted: {real_case_id}")
        return real_case_id

    def get_pending_review_cases(self, limit: int = 50) -> List[Dict]:
        """Get active review cases for the Human Review workflow.

        STEP 5: Joins to classification_observations via
        last_observation_id to surface current confidence/reasoning/
        device_name for display, without duplicating that data into
        review_cases itself.
        """
        conn = self.connect()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT
                rc.id AS case_id,
                rc.device_id,
                rc.classifier_name,
                rc.hypothesis_category,
                rc.status,
                rc.decision,
                rc.corrected_category,
                rc.created_at AS case_created_at,
                rc.updated_at AS case_updated_at,
                co.id AS observation_id,
                co.device_name,
                co.hypothesis_confidence,
                co.hypothesis_reasoning
            FROM review_cases rc
            JOIN classification_observations co ON co.id = rc.last_observation_id
            WHERE rc.status = 'pending'
            ORDER BY rc.updated_at DESC
            LIMIT ?
        ''', (limit,))

        return [dict(row) for row in cursor.fetchall()]

    def resolve_review_case(
        self,
        case_id: str,
        expected_observation_id: str,
        decision: str,
        reason: Optional[str] = None,
        corrected_category: Optional[str] = None,
        reviewed_by: str = 'human',
    ) -> str:
        """Resolve a review case, guarding against stale reviews.

        STEP 5: expected_observation_id must match the case's current
        last_observation_id or the resolution is refused ('stale') - if a
        newer HA event refreshed the case since the reviewer loaded it,
        the human would otherwise be recorded as having reviewed an
        observation they never saw. The check-and-update is a single
        UPDATE ... WHERE (case_id AND last_observation_id both matched),
        closing the race window entirely rather than narrowing it.

        STEP 6.3: human decisions are immutable. Only a 'pending' case can
        be resolved; a resolved case is refused ('already_resolved') and
        its decision, corrected_category and decided_at stay untouched.
        The dual-write likewise only labels an observation whose
        human_decision is still NULL. Revising an earlier human decision,
        if ever needed, must be a separate explicit operation that
        preserves the original - never an in-place overwrite.

        decision is validated before any write. The review_cases UPDATE
        and the classification_observations dual-write are one logical
        operation via explicit try/except/rollback; the second UPDATE's
        rowcount is verified too, so the case is never left 'resolved' if
        the dual-write touched zero or more than one row.

        Returns:
            'resolved'         - resolved successfully
            'already_resolved' - case was not pending; nothing written
            'stale'            - last_observation_id had moved on; nothing written
            'not_found'        - no such case_id

        Raises:
            ValueError: invalid decision, or corrected_category
                present/missing inconsistently with decision
        """
        valid_decisions = {"approved", "rejected", "corrected"}
        if decision not in valid_decisions:
            raise ValueError(
                f"decision must be one of {valid_decisions}, got {decision!r}"
            )
        if decision == "corrected":
            if not corrected_category:
                raise ValueError(
                    "corrected_category is required when decision='corrected'"
                )
        else:
            if corrected_category is not None:
                raise ValueError(
                    f"corrected_category must be None when decision={decision!r}, "
                    f"got {corrected_category!r}"
                )

        conn = self.connect()
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        try:
            cursor.execute('''
                UPDATE review_cases
                SET status = 'resolved',
                    decision = ?,
                    corrected_category = ?,
                    decided_at = ?,
                    updated_at = ?
                WHERE id = ? AND last_observation_id = ? AND status = 'pending'
            ''', (decision, corrected_category, now, now, case_id, expected_observation_id))

            if cursor.rowcount == 0:
                conn.rollback()
                cursor.execute('SELECT status FROM review_cases WHERE id = ?', (case_id,))
                row = cursor.fetchone()
                if row is None:
                    print(f"[STORAGE] WARNING: resolve_review_case found no case for {case_id}")
                    return 'not_found'
                if row['status'] != 'pending':
                    print(f"[STORAGE] WARNING: resolve_review_case refused - {case_id} "
                          f"is already {row['status']}; existing decision kept")
                    return 'already_resolved'
                print(f"[STORAGE] WARNING: resolve_review_case stale - {case_id} "
                      f"no longer points at {expected_observation_id}")
                return 'stale'

            cursor.execute('''
                UPDATE classification_observations
                SET review_status = 'reviewed',
                    human_decision = ?,
                    reviewed_at = ?,
                    review_reason = ?,
                    corrected_category = ?,
                    reviewed_by = ?
                WHERE id = ? AND human_decision IS NULL
            ''', (decision, now, reason, corrected_category, reviewed_by, expected_observation_id))

            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Expected exactly one observation {expected_observation_id} "
                    f"to be updated, got {cursor.rowcount}"
                )

            conn.commit()
            print(f"[STORAGE] Review case resolved: {case_id} ({decision})")
            return 'resolved'

        except Exception as exc:
            conn.rollback()
            print(f"[STORAGE] ERROR: resolve_review_case failed, rolled back: {exc}")
            raise

    def backfill_review_cases(self) -> Dict[str, int]:
        """Idempotent backfill AND crash-recovery reconciliation.

        STEP 5: derives TWO independent pieces of state per logical key
        (device_id, classifier_name, hypothesis_category):

        1. CURRENT EVIDENCE POINTER (last_observation_id) - the newest
           observation regardless of review_status, ORDER BY created_at
           DESC, id DESC.

        2. HUMAN WORKFLOW STATE (status/decision/corrected_category/
           decided_at) - derived ONLY from the newest genuinely reviewed
           observation for that key (review_status='reviewed'), ORDER BY
           reviewed_at DESC, created_at DESC, id DESC. If no observation
           for the key was ever reviewed, workflow state is 'pending'/NULL.

        Deriving both from the same "newest row" would incorrectly reopen
        a case a human already resolved, whenever a later noisy
        observation happens to be pending. Example: A1 reviewed/approved,
        A2 pending, A3 pending -> migrates to status='resolved',
        decision='approved' (from A1), last_observation_id=A3 (freshest
        evidence) - not status='pending' just because A3 is newest.

        For EXISTING review_cases (reconciliation path, e.g. after a
        crash between committing an observation and calling
        upsert_review_case() for it): only last_observation_id/updated_at
        are refreshed if stale. status/decision/corrected_category/
        decided_at are NEVER touched here - only upsert_review_case()/
        resolve_review_case() at runtime are allowed to change workflow
        state.

        Safe to call on every startup.

        Returns:
            {"created": N, "reconciled": M}
        """
        conn = self.connect()
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # 1. Current evidence pointer: newest observation per key,
        #    regardless of review_status.
        cursor.execute('''
            SELECT device_id, classifier_name, hypothesis_category,
                   id AS observation_id
            FROM (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY device_id, classifier_name, hypothesis_category
                        ORDER BY created_at DESC, id DESC
                    ) AS rn
                FROM classification_observations
            )
            WHERE rn = 1
        ''')
        latest_evidence = {
            (r['device_id'], r['classifier_name'], r['hypothesis_category']): r['observation_id']
            for r in cursor.fetchall()
        }

        # 2. Human workflow state: newest REVIEWED observation per key, if any.
        cursor.execute('''
            SELECT device_id, classifier_name, hypothesis_category,
                   human_decision, corrected_category, reviewed_at
            FROM (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY device_id, classifier_name, hypothesis_category
                        ORDER BY reviewed_at DESC, created_at DESC, id DESC
                    ) AS rn
                FROM classification_observations
                WHERE review_status = 'reviewed'
            )
            WHERE rn = 1
        ''')
        latest_decision = {
            (r['device_id'], r['classifier_name'], r['hypothesis_category']): r
            for r in cursor.fetchall()
        }

        created = 0
        reconciled = 0

        for key, observation_id in latest_evidence.items():
            device_id, classifier_name, hypothesis_category = key

            cursor.execute('''
                SELECT id, last_observation_id FROM review_cases
                WHERE device_id = ? AND classifier_name = ? AND hypothesis_category = ?
            ''', key)
            existing = cursor.fetchone()

            if existing is None:
                from uuid import uuid4
                decision_row = latest_decision.get(key)
                if decision_row is not None:
                    status = 'resolved'
                    decision = decision_row['human_decision']
                    corrected_category = decision_row['corrected_category']
                    decided_at = decision_row['reviewed_at']
                else:
                    status = 'pending'
                    decision = None
                    corrected_category = None
                    decided_at = None

                case_id = f"case_{uuid4().hex[:8]}"
                cursor.execute('''
                    INSERT INTO review_cases (
                        id, device_id, classifier_name, hypothesis_category,
                        status, decision, corrected_category,
                        last_observation_id, created_at, updated_at, decided_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    case_id, device_id, classifier_name, hypothesis_category,
                    status, decision, corrected_category,
                    observation_id, now, now, decided_at,
                ))
                created += 1

            elif existing['last_observation_id'] != observation_id:
                # Reconciliation only: refresh the evidence pointer, never
                # touch workflow state on an existing case.
                cursor.execute('''
                    UPDATE review_cases
                    SET last_observation_id = ?, updated_at = ?
                    WHERE id = ?
                ''', (observation_id, now, existing['id']))
                reconciled += 1

        conn.commit()
        print(f"[STORAGE] backfill_review_cases: {created} created, {reconciled} reconciled")
        return {"created": created, "reconciled": reconciled}

    def get_pending_reviews(
        self,
        limit: int = 50,
        include_reviewed: bool = False,
    ) -> List[Dict]:
        """Get classification observations for the Learning Loop.

        STEP 3B: Data layer - single source of truth for CLI, future UI,
        stats, and export. All Learning Loop consumers should call this
        method rather than writing their own SQL against
        classification_observations.

        NOTE (STEP 5): This method still reads/returns raw observation
        rows and is retained for get_review_analytics()/history use.
        The primary Human Review workflow now uses
        get_pending_review_cases() instead, which is deduplicated to one
        active case per logical hypothesis.

        Args:
            limit: maximum number of observations to return
            include_reviewed: if False (default), only observations with
                review_status='pending'. If True, returns both pending
                and already-reviewed observations.

        Returns:
            List of dicts with observation fields, most recent first:
            id, observation_group_id, device_id, device_name,
            classifier_name, hypothesis_category, hypothesis_confidence,
            hypothesis_reasoning, review_status, human_decision,
            reviewed_at, review_reason, corrected_category, reviewed_by,
            created_at
        """
        conn = self.connect()
        cursor = conn.cursor()

        base_query = '''
            SELECT
                id,
                observation_group_id,
                device_id,
                device_name,
                classifier_name,
                hypothesis_category,
                hypothesis_confidence,
                hypothesis_reasoning,
                review_status,
                human_decision,
                reviewed_at,
                review_reason,
                corrected_category,
                reviewed_by,
                created_at
            FROM classification_observations
        '''

        if include_reviewed:
            cursor.execute(
                base_query + ' ORDER BY created_at DESC LIMIT ?',
                (limit,)
            )
        else:
            cursor.execute(
                base_query
                + " WHERE review_status = 'pending'"
                + ' ORDER BY created_at DESC LIMIT ?',
                (limit,)
            )

        return [dict(row) for row in cursor.fetchall()]

    def count_pending_reviews(self) -> int:
        """Count active review cases awaiting human decision.

        STEP 5: Counts review_cases.status='pending', not raw
        classification_observations rows - a device with 5 repeated,
        unresolved observations of the same hypothesis counts once here,
        not five times.

        STEP 4A.2 thread-safety note (preserved under STEP 5): this
        method uses its own short-lived connection instead of
        self.connect(). It is the one Storage method invoked via
        asyncio.to_thread() (see ha_sensor.update_pending_reviews(),
        called from core.py and main.py). sqlite3 connections are
        thread-affine by default (check_same_thread=True) and
        to_thread() runs on the executor's thread pool - a different
        thread than whichever one first created self.connection via
        self.connect(). Reusing the cached connection across threads
        would raise:
            sqlite3.ProgrammingError: SQLite objects created in a
            thread can only be used in that same thread
        A plain `with sqlite3.connect(...) as conn:` does NOT avoid a
        connection leak here - Connection's context manager only
        commits/rolls back the transaction on exit, it does not close
        the connection. Explicit try/finally close() is used instead.

        Returns:
            Count of review_cases with status='pending'
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute('''
                SELECT COUNT(*) FROM review_cases
                WHERE status = 'pending'
            ''')
            return int(cursor.fetchone()[0])
        finally:
            conn.close()

    def get_correction_patterns(self) -> List[Dict]:
        """Aggregate human-corrected classifications into (classifier,
        predicted category, corrected category) patterns with sample size.

        STEP 6 (first increment, "Level 1" per docs/STEP5_ARCHITECTURE.md -
        renamed STEP 6 in this repo's history since the review_cases
        idempotency work absorbed the "STEP 5" name; the design intent is
        unchanged): turns accumulated human decisions into statistics a
        human can act on. Does NOT modify classifiers, confidence, or
        rules - this method is read-only and purely observational.

        CRITICAL - unit of analysis (established during the STEP 6 audit,
        with direct production evidence from a "T & H Sensor" device):

        review_cases.last_observation_id is a freshness pointer, NOT a
        decision pointer. It advances after a case is resolved whenever a
        later, unlabelled observation for the same logical key arrives.
        This method therefore NEVER joins through review_cases or
        last_observation_id. The only correct source for "what did a
        human actually decide" is classification_observations rows where
        human_decision IS NOT NULL, taken directly.

        Raw observation count is NOT sample size. A single device can
        produce many repeated classification_observations rows for one
        still-or-already-reviewed logical hypothesis (confirmed in
        production: 10 raw observations backing only 1 actual human
        judgment). sample_size here counts independent labelled logical
        cases only - never raw rows.

        Integrity precondition: the current architecture (resolve_review_
        case()'s dual-write, scoped to exactly one observation row per
        logical key at decision time) should make it impossible for more
        than one classification_observations row per (device_id,
        classifier_name, hypothesis_category) to carry a non-NULL
        human_decision at the same time. This method verifies that
        precondition before computing anything. If violated, it raises
        rather than guessing which row represents the real judgment
        (picking "newest" would be arbitrary; silently counting both or
        skipping would corrupt or hide the statistic) - this can currently
        only happen via the legacy approve_observation()/
        reject_observation()/correct_observation() methods, which bypass
        review_cases entirely and are not exercised by any current call
        site, but remain reachable.

        Returns:
            List of dicts, most-corrected first:
            [{
                "classifier_name": str,
                "hypothesis_category": str,
                "corrected_category": str,
                "correction_count": int,
                "sample_size": int,  # all labelled judgments (approved +
                                     # rejected + corrected) for this
                                     # (classifier_name, hypothesis_category)
                                     # across all devices - the denominator
                                     # for correction_count, not the same
                                     # thing as correction_count's own count
            }, ...]

        Raises:
            RuntimeError: if more than one classification_observations
                row shares the same (device_id, classifier_name,
                hypothesis_category) with human_decision IS NOT NULL.
                No data is modified either way.
        """
        conn = self.connect()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT device_id, classifier_name, hypothesis_category, COUNT(*) AS cnt
            FROM classification_observations
            WHERE human_decision IS NOT NULL
            GROUP BY device_id, classifier_name, hypothesis_category
            HAVING COUNT(*) > 1
        ''')
        violations = cursor.fetchall()
        if violations:
            details = [
                f"({v['device_id']}, {v['classifier_name']}, "
                f"{v['hypothesis_category']}): {v['cnt']} labelled rows"
                for v in violations
            ]
            raise RuntimeError(
                "get_correction_patterns(): integrity assumption violated - "
                f"{len(violations)} logical case(s) have more than one "
                "labelled observation, which should be impossible under "
                "the current resolve_review_case() dual-write discipline. "
                f"Details: {details}"
            )

        cursor.execute('''
            SELECT classifier_name, hypothesis_category, COUNT(*) AS sample_size
            FROM classification_observations
            WHERE human_decision IS NOT NULL
            GROUP BY classifier_name, hypothesis_category
        ''')
        sample_sizes = {
            (row['classifier_name'], row['hypothesis_category']): row['sample_size']
            for row in cursor.fetchall()
        }

        cursor.execute('''
            SELECT classifier_name, hypothesis_category, corrected_category,
                   COUNT(*) AS correction_count
            FROM classification_observations
            WHERE human_decision = 'corrected'
            GROUP BY classifier_name, hypothesis_category, corrected_category
            ORDER BY correction_count DESC, classifier_name ASC,
                     hypothesis_category ASC, corrected_category ASC
        ''')

        return [
            {
                "classifier_name": row["classifier_name"],
                "hypothesis_category": row["hypothesis_category"],
                "corrected_category": row["corrected_category"],
                "correction_count": row["correction_count"],
                "sample_size": sample_sizes[
                    (row["classifier_name"], row["hypothesis_category"])
                ],
            }
            for row in cursor.fetchall()
        ]

    def get_review_analytics(self) -> Dict:
        """Aggregate review decisions into a summary, per-classifier
        breakdown, most-corrected categories, and problematic devices.

        STEP 4B.1: Review Analytics data layer.

        Uses its own short-lived connection (same reasoning as
        count_pending_reviews() - see that method's docstring for why
        self.connect()'s cached connection is not used here: this may
        eventually be called from the same to_thread/CLI contexts).

        NOTE (STEP 5): This continues to read classification_observations
        directly and is unaffected by the review_cases workflow layer -
        resolve_review_case() dual-writes review_status/human_decision/
        reviewed_at/review_reason/corrected_category/reviewed_by onto the
        specific observation row it resolved, so this method's queries
        keep working unchanged. "pending" here means "observation rows
        never reviewed", which is a different number than
        sensor.cognitive_core_pending_reviews (active review_cases) -
        these are two distinct, intentionally separate metrics.

        Definitions:
        - reviewed = human_decision is not NULL (approved, rejected,
          or corrected). pending observations are excluded from all
          rate calculations.
        - approval_rate = approved / reviewed. None (not 0.0) when
          reviewed == 0, so "no data yet" is never confused with
          "0% approval".
        - rejected and corrected both count as incorrect classifications
          for the purpose of approval_rate; corrected additionally
          contributes to most_corrected_categories.
        - most_corrected_categories groups by hypothesis_category (the
          classifier's original, wrong guess) where human_decision =
          'corrected', not by corrected_category - this answers "which
          category does the classifier most often get wrong", not
          "which category do things get corrected to".
        - problematic_devices groups by device_id (with device_name
          for display) where human_decision is 'rejected' or
          'corrected' - i.e. any incorrect classification, regardless
          of which classifier or category was involved.
        - Both breakdown lists are capped at the top 10 by count to
          avoid returning an unbounded list on a large history.

        Returns:
            {
                "summary": {
                    "total_observations": int,
                    "pending": int,
                    "reviewed": int,
                    "approved": int,
                    "rejected": int,
                    "corrected": int,
                    "approval_rate": float | None,
                },
                "by_classifier": [
                    {
                        "classifier_name": str,
                        "reviewed": int,
                        "approved": int,
                        "rejected": int,
                        "corrected": int,
                        "approval_rate": float | None,
                    },
                    ...
                ],
                "most_corrected_categories": [
                    {"hypothesis_category": str, "correction_count": int},
                    ...
                ],
                "problematic_devices": [
                    {
                        "device_id": str,
                        "device_name": str,
                        "incorrect_count": int,
                    },
                    ...
                ],
            }
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            summary = self._analytics_summary(conn)
            by_classifier = self._analytics_by_classifier(conn)
            most_corrected = self._analytics_most_corrected_categories(conn)
            problematic = self._analytics_problematic_devices(conn)

            return {
                "summary": summary,
                "by_classifier": by_classifier,
                "most_corrected_categories": most_corrected,
                "problematic_devices": problematic,
            }
        finally:
            conn.close()

    @staticmethod
    def _approval_rate(approved: int, reviewed: int) -> Optional[float]:
        """Shared approval-rate calculation: None when reviewed == 0,
        so callers never have to guess whether 0.0 means "0% approval"
        or "no data yet"."""
        if reviewed == 0:
            return None
        return approved / reviewed

    def _analytics_summary(self, conn) -> Dict:
        row = conn.execute('''
            SELECT
                COUNT(*) AS total_observations,
                SUM(CASE WHEN review_status = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN human_decision IS NOT NULL THEN 1 ELSE 0 END) AS reviewed,
                SUM(CASE WHEN human_decision = 'approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN human_decision = 'rejected' THEN 1 ELSE 0 END) AS rejected,
                SUM(CASE WHEN human_decision = 'corrected' THEN 1 ELSE 0 END) AS corrected
            FROM classification_observations
        ''').fetchone()

        total_observations = row["total_observations"] or 0
        pending = row["pending"] or 0
        reviewed = row["reviewed"] or 0
        approved = row["approved"] or 0
        rejected = row["rejected"] or 0
        corrected = row["corrected"] or 0

        return {
            "total_observations": total_observations,
            "pending": pending,
            "reviewed": reviewed,
            "approved": approved,
            "rejected": rejected,
            "corrected": corrected,
            "approval_rate": self._approval_rate(approved, reviewed),
        }

    def _analytics_by_classifier(self, conn) -> list:
        rows = conn.execute('''
            SELECT
                classifier_name,
                SUM(CASE WHEN human_decision IS NOT NULL THEN 1 ELSE 0 END) AS reviewed,
                SUM(CASE WHEN human_decision = 'approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN human_decision = 'rejected' THEN 1 ELSE 0 END) AS rejected,
                SUM(CASE WHEN human_decision = 'corrected' THEN 1 ELSE 0 END) AS corrected
            FROM classification_observations
            GROUP BY classifier_name
            ORDER BY classifier_name
        ''').fetchall()

        result = []
        for row in rows:
            reviewed = row["reviewed"] or 0
            approved = row["approved"] or 0
            result.append({
                "classifier_name": row["classifier_name"],
                "reviewed": reviewed,
                "approved": approved,
                "rejected": row["rejected"] or 0,
                "corrected": row["corrected"] or 0,
                "approval_rate": self._approval_rate(approved, reviewed),
            })
        return result

    def _analytics_most_corrected_categories(self, conn, limit: int = 10) -> list:
        # ORDER BY includes a secondary ASC key on hypothesis_category
        # so ties in correction_count produce a deterministic order -
        # otherwise SQLite's tie-breaking is unspecified, which would
        # make both tests and CLI output vary between runs.
        rows = conn.execute('''
            SELECT
                hypothesis_category,
                COUNT(*) AS correction_count
            FROM classification_observations
            WHERE human_decision = 'corrected'
            GROUP BY hypothesis_category
            ORDER BY correction_count DESC, hypothesis_category ASC
            LIMIT ?
        ''', (limit,)).fetchall()

        return [
            {
                "hypothesis_category": row["hypothesis_category"],
                "correction_count": row["correction_count"],
            }
            for row in rows
        ]

    def _analytics_problematic_devices(self, conn, limit: int = 10) -> list:
        # TODO(STEP 4C+): incorrect_count alone can't distinguish "2
        # wrong out of 2 attempts" from "2 wrong out of 150 attempts".
        # Consider adding approved_count and a per-device approval_rate
        # here, mirroring _analytics_by_classifier(), once there's a
        # concrete use case (device-level trend dashboard, etc).
        #
        # Secondary ASC sort on device_name for the same determinism
        # reason as _analytics_most_corrected_categories() above.
        rows = conn.execute('''
            SELECT
                device_id,
                device_name,
                COUNT(*) AS incorrect_count
            FROM classification_observations
            WHERE human_decision IN ('rejected', 'corrected')
            GROUP BY device_id
            ORDER BY incorrect_count DESC, device_name ASC
            LIMIT ?
        ''', (limit,)).fetchall()

        return [
            {
                "device_id": row["device_id"],
                "device_name": row["device_name"],
                "incorrect_count": row["incorrect_count"],
            }
            for row in rows
        ]

    # -- Legacy observation-level decisions ---------------------------------
    #
    # LEGACY / DEPRECATED (STEP 5, hardened in STEP 6.4): superseded by
    # resolve_review_case() for the Human Review workflow. No caller in
    # src/ uses them; they are kept only because a manual script outside
    # the repository might. Like resolve_review_case() (STEP 6.3), they
    # never overwrite an existing human decision: only an observation with
    # human_decision IS NULL can be decided, a second decision is refused
    # as 'already_resolved' and leaves every decision field unchanged.
    # Returned DecisionResult truthiness matches the previous bool contract.

    def approve_observation(
        self,
        observation_id: str,
        reason: Optional[str] = None,
        reviewed_by: str = "human",
    ) -> DecisionResult:
        """DEPRECATED: mark an observation as approved (classifier was
        correct). Use resolve_review_case() instead.

        Returns:
            DecisionResult - 'resolved' (truthy), or 'already_resolved' /
            'not_found' (both falsy, nothing written).
        """
        return self._record_legacy_decision(
            "approve_observation", observation_id, "approved",
            None, reason, reviewed_by,
        )

    def reject_observation(
        self,
        observation_id: str,
        reason: Optional[str] = None,
        reviewed_by: str = "human",
    ) -> DecisionResult:
        """DEPRECATED: mark an observation as rejected (classifier was
        wrong, no correct category supplied). Use resolve_review_case()
        instead.

        Returns:
            DecisionResult - 'resolved' (truthy), or 'already_resolved' /
            'not_found' (both falsy, nothing written).
        """
        return self._record_legacy_decision(
            "reject_observation", observation_id, "rejected",
            None, reason, reviewed_by,
        )

    def correct_observation(
        self,
        observation_id: str,
        corrected_category: str,
        reason: Optional[str] = None,
        reviewed_by: str = "human",
    ) -> DecisionResult:
        """DEPRECATED: mark an observation as corrected - classifier was
        wrong and the human supplies the correct category. Use
        resolve_review_case() instead.

        The original hypothesis_category is preserved as-is;
        corrected_category stores the human-supplied answer separately.

        Returns:
            DecisionResult - 'resolved' (truthy), or 'already_resolved' /
            'not_found' (both falsy, nothing written).
        """
        return self._record_legacy_decision(
            "correct_observation", observation_id, "corrected",
            corrected_category, reason, reviewed_by,
        )

    def _record_legacy_decision(
        self,
        method: str,
        observation_id: str,
        decision: str,
        corrected_category: Optional[str],
        reason: Optional[str],
        reviewed_by: str,
    ) -> DecisionResult:
        """Shared write path for the three legacy decision methods."""
        warnings.warn(
            f"Storage.{method}() is deprecated; use resolve_review_case()",
            DeprecationWarning,
            stacklevel=3,
        )
        conn = self.connect()
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        cursor.execute('''
            UPDATE classification_observations
            SET
                review_status = 'reviewed',
                human_decision = ?,
                corrected_category = ?,
                reviewed_at = ?,
                review_reason = ?,
                reviewed_by = ?
            WHERE id = ? AND human_decision IS NULL
        ''', (decision, corrected_category, now, reason, reviewed_by, observation_id))

        if cursor.rowcount == 0:
            conn.rollback()
            cursor.execute(
                "SELECT human_decision FROM classification_observations WHERE id = ?",
                (observation_id,),
            )
            row = cursor.fetchone()
            if row is None:
                print(f"[STORAGE] WARNING: {method} found no row for {observation_id}")
                return DecisionResult("not_found")
            print(f"[STORAGE] WARNING: {method} refused - {observation_id} already "
                  f"has human_decision={row['human_decision']!r}; existing decision kept")
            return DecisionResult("already_resolved")

        conn.commit()
        print(f"[STORAGE] Observation {decision}: {observation_id}")
        return DecisionResult("resolved")

    def insert_environmental_reading(self, asset_id: str, data: Dict) -> int:
        """Insert environmental sensor reading"""
        conn = self.connect()
        cursor = conn.cursor()

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        timestamp = data.get('timestamp', now)

        cursor.execute('''
            INSERT INTO environmental_readings (
                asset_id, timestamp,
                temperature, humidity, illuminance, motion_detected, battery_percent,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            asset_id,
            timestamp,
            data.get('temperature'),
            data.get('humidity'),
            data.get('illuminance'),
            data.get('motion_detected'),
            data.get('battery_percent'),
            now
        ))

        conn.commit()
        reading_id = cursor.lastrowid
        print(f"[STORAGE] Saved environmental reading: asset={asset_id}, id={reading_id}")
        return reading_id

    def get_latest_reading(self, asset_id: str) -> Optional[Dict]:
        """Get latest environmental reading for asset"""
        conn = self.connect()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT
                id, asset_id, timestamp,
                temperature, humidity, illuminance, motion_detected, battery_percent
            FROM environmental_readings
            WHERE asset_id = ?
            ORDER BY timestamp DESC
            LIMIT 1
        ''', (asset_id,))

        row = cursor.fetchone()
        if row:
            return dict(row)
        return None

    def get_readings_range(self, asset_id: str, start: str, end: str) -> List[Dict]:
        """Get environmental readings in time range"""
        conn = self.connect()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT
                id, asset_id, timestamp,
                temperature, humidity, illuminance, motion_detected, battery_percent
            FROM environmental_readings
            WHERE asset_id = ? AND timestamp BETWEEN ? AND ?
            ORDER BY timestamp DESC
        ''', (asset_id, start, end))

        items = []
        for row in cursor.fetchall():
            items.append(dict(row))

        return items
