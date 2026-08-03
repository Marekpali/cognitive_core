# src/storage.py

import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, List
import json

class Storage:
    """SQLite-based operational data storage"""

    def __init__(self, db_path: Path = Path("data/core.db")):
        self.db_path = db_path
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

        # Review queue table
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

        # STEP 3: Ensure review columns exist (idempotent migration)
        self._ensure_review_columns(conn)

        # Commit ALL changes in one transaction
        conn.commit()
        conn.close()

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

    def _ensure_review_columns(self, conn) -> None:
        """STEP 3: Idempotent migration - add review columns if missing.

        Checks which columns exist, adds only the missing ones.
        Safe to call on every startup - running twice does nothing on
        the second run.

        Columns added to classification_observations:
        - review_status: 'pending' | 'reviewed' (default 'pending')
        - human_decision: 'approved' | 'rejected' | 'corrected' | NULL
        - reviewed_at: ISO timestamp when reviewed, NULL if pending
        - review_reason: Optional text notes from reviewer
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
        }

        added = []
        for name, definition in columns.items():
            if name not in existing:
                cursor.execute(
                    f"ALTER TABLE classification_observations "
                    f"ADD COLUMN {name} {definition}"
                )
                added.append(name)
                print(f"[STORAGE] Added column: {name}")

        if added:
            print(f"[STORAGE] Migrated {len(added)} column(s)")
        else:
            print("[STORAGE] All review columns already present")

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_classification_observations_review_status
            ON classification_observations(review_status)
        """)
        print("[STORAGE] Index verified: idx_classification_observations_review_status")

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

        now = datetime.utcnow().isoformat() + 'Z'

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

    def add_to_review_queue(self, asset_id: str) -> str:
        """Add to review queue"""
        from uuid import uuid4

        conn = self.connect()
        cursor = conn.cursor()

        review_id = f"review_{uuid4().hex[:8]}"
        now = datetime.utcnow().isoformat() + 'Z'

        cursor.execute('''
            INSERT INTO review_queue (id, asset_id, status, created_at)
            VALUES (?, ?, ?, ?)
        ''', (review_id, asset_id, 'pending', now))

        conn.commit()
        print(f"[STORAGE] Added to review queue: {review_id}")
        return review_id

    def get_pending_reviews(self) -> List[Dict]:
        """Get pending reviews"""
        conn = self.connect()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT
                r.id as review_id,
                a.id as asset_id,
                a.name,
                a.hypothesis_category,
                a.hypothesis_confidence,
                a.hypothesis_reasoning
            FROM review_queue r
            JOIN assets a ON r.asset_id = a.id
            WHERE r.status = 'pending'
            ORDER BY r.created_at DESC
        ''')

        items = []
        for row in cursor.fetchall():
            items.append(dict(row))

        return items

    def approve_review(self, review_id: str) -> bool:
        """Approve review"""
        conn = self.connect()
        cursor = conn.cursor()

        now = datetime.utcnow().isoformat() + 'Z'

        cursor.execute('''
            UPDATE review_queue
            SET status = 'approved', decision = 'approved', decided_at = ?
            WHERE id = ?
        ''', (now, review_id))

        cursor.execute('''
            SELECT asset_id FROM review_queue WHERE id = ?
        ''', (review_id,))
        row = cursor.fetchone()

        if row:
            asset_id = row['asset_id']
            cursor.execute('''
                UPDATE assets
                SET lifecycle_state = 'confirmed', updated_at = ?
                WHERE id = ?
            ''', (now, asset_id))

        conn.commit()
        print(f"[STORAGE] Approved review: {review_id}")
        return True

    def reject_review(self, review_id: str) -> bool:
        """Reject review"""
        conn = self.connect()
        cursor = conn.cursor()

        now = datetime.utcnow().isoformat() + 'Z'

        cursor.execute('''
            UPDATE review_queue
            SET status = 'rejected', decision = 'rejected', decided_at = ?
            WHERE id = ?
        ''', (now, review_id))

        conn.commit()
        print(f"[STORAGE] Rejected review: {review_id}")
        return True

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
        from uuid import uuid4

        conn = self.connect()
        cursor = conn.cursor()

        observation_id = f"obs_{uuid4().hex[:12]}"
        now = datetime.utcnow().isoformat() + 'Z'

        try:
            cursor.execute('''
                INSERT INTO classification_observations (
                    id,
                    observation_group_id,
                    device_id,
                    classifier_name,
                    hypothesis_category,
                    hypothesis_confidence,
                    hypothesis_reasoning,
                    device_name,
                    device_model,
                    device_manufacturer,
                    device_source_adapter,
                    device_entity_count,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                observation_id,
                payload["observation_group_id"],
                payload["device_id"],
                payload["classifier_name"],
                payload["hypothesis_category"],
                payload["hypothesis_confidence"],
                payload["hypothesis_reasoning"],
                payload["device_name"],
                payload["device_model"],
                payload["device_manufacturer"],
                payload["device_source_adapter"],
                payload["device_entity_count"],
                now
            ))

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

    def insert_environmental_reading(self, asset_id: str, data: Dict) -> int:
        """Insert environmental sensor reading"""
        conn = self.connect()
        cursor = conn.cursor()

        now = datetime.utcnow().isoformat() + 'Z'
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
