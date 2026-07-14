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
        
        # Indexes
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_assets_category '
            'ON assets(hypothesis_category)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_review_status '
            'ON review_queue(status)'
        )
        
        conn.commit()
        conn.close()
    
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
