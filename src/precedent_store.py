"""STEP 7a persistence: precedent annotations, their evidence snapshot and the
layer audit (docs/STEP7_ARCHITECTURE.md, docs/STEP7A_IMPLEMENTATION_PLAN.md).

- precedent_annotations / precedent_annotation_evidence: what earlier human
  decisions suggested about an observation, with a value-copying snapshot
  of the evidence. Written once, never refreshed.
- precedent_audit: the layer's cut-off (`layer_started`, exactly one row)
  and every failed annotation attempt (`annotation_failed`).

All three are append-only. Everything here works on a cursor and never
commits, except ensure_layer_started().
"""

import json
import sqlite3
from datetime import datetime
from typing import Optional
from uuid import uuid4

from src.learning.precedent import Annotation, Decision

ANNOTATION_TRIGGERS = ("observation", "bootstrap_pending")
PRECEDENT_TABLES = ("precedent_annotations", "precedent_annotation_evidence", "precedent_audit")
PRECEDENT_TRIGGERS = tuple(
    f"trg_{table}_no_{op}" for table in PRECEDENT_TABLES for op in ("update", "delete"))
LAYER_STARTED = "layer_started"
ANNOTATION_FAILED = "annotation_failed"
ERROR_MESSAGE_LIMIT = 200


def _instant(timestamp: str) -> datetime:
    """Timestamps are compared as instants, not as strings: isoformat()
    drops the fraction when it is zero, which breaks lexical order."""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def ensure_schema(cursor: sqlite3.Cursor) -> None:
    """Idempotent. Must run after classification_observations exists."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS precedent_annotations (
            id                   TEXT PRIMARY KEY,
            observation_id       TEXT NOT NULL REFERENCES classification_observations(id),
            memory_type          TEXT NOT NULL
                CHECK (memory_type IN ('device_precedent', 'class_pattern')),
            annotation_trigger   TEXT NOT NULL
                CHECK (annotation_trigger IN ('observation', 'bootstrap_pending')),
            device_id            TEXT NOT NULL,
            classifier_name      TEXT NOT NULL,
            hypothesis_category  TEXT NOT NULL,
            result               TEXT NOT NULL CHECK (result IN ('suggestion', 'ambiguous')),
            suggested_outcome    TEXT,
            suggested_category   TEXT,
            evidence_count       INTEGER NOT NULL,
            sample_size          INTEGER NOT NULL,
            correction_count     INTEGER NOT NULL,
            outcome_distribution TEXT NOT NULL,
            evidence_maturity    TEXT NOT NULL,
            evidence_as_of       TEXT NOT NULL,
            evidence_digest      TEXT NOT NULL,
            policy_version       TEXT NOT NULL,
            created_at           TEXT NOT NULL,
            UNIQUE(observation_id, memory_type)
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_precedent_annotations_key
        ON precedent_annotations(device_id, classifier_name, hypothesis_category)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS precedent_annotation_evidence (
            annotation_id           TEXT NOT NULL REFERENCES precedent_annotations(id),
            review_case_id          TEXT,
            labelled_observation_id TEXT NOT NULL,
            device_id               TEXT NOT NULL,
            human_decision          TEXT NOT NULL,
            corrected_category      TEXT,
            decided_at              TEXT NOT NULL,
            PRIMARY KEY (annotation_id, labelled_observation_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS precedent_audit (
            id             TEXT PRIMARY KEY,
            event          TEXT NOT NULL CHECK (event IN ('layer_started', 'annotation_failed')),
            source         TEXT CHECK (source IN ('observation', 'bootstrap_pending')),
            observation_id TEXT,
            review_case_id TEXT,
            error_class    TEXT,
            error_message  TEXT,
            policy_version TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            CHECK (
                (event = 'layer_started'     AND source IS NULL)
             OR (event = 'annotation_failed' AND source IS NOT NULL
                 AND source IN ('observation', 'bootstrap_pending'))
            )
        )
    """)
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_precedent_layer_started
        ON precedent_audit(event) WHERE event = 'layer_started'
    """)
    for table in PRECEDENT_TABLES:
        for op in ("update", "delete"):
            cursor.execute(f"""
                CREATE TRIGGER IF NOT EXISTS trg_{table}_no_{op}
                BEFORE {op.upper()} ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'APPEND_ONLY: {table} rows cannot be {op}d');
                END
            """)
    print(f"[STORAGE] STEP 7a schema verified ({len(PRECEDENT_TRIGGERS)} triggers)")


def layer_started_at(cursor) -> Optional[str]:
    """The layer's cut-off, or None if it has never run in shadow."""
    row = cursor.execute(
        "SELECT created_at FROM precedent_audit WHERE event = ?", (LAYER_STARTED,)).fetchone()
    return row[0] if row else None


def ensure_layer_started(conn: sqlite3.Connection, now: str, policy_version: str) -> str:
    """Write the cut-off once (Core only, first start in shadow). Commits."""
    conn.execute(
        "INSERT OR IGNORE INTO precedent_audit (id, event, policy_version, created_at) "
        "VALUES (?, ?, ?, ?)", (f"pau_{uuid4().hex[:12]}", LAYER_STARTED, policy_version, now))
    conn.commit()
    return layer_started_at(conn)


def labelled_decisions(cursor, classifier_name: str, hypothesis_category: str, before: str,
                       device_id: Optional[str] = None,
                       exclude_device_id: Optional[str] = None) -> list:
    """Human decisions for (classifier, category) made strictly before
    `before`, read directly from the labelled observation rows - never
    through review_cases.last_observation_id, which is a freshness pointer
    (same rule as Storage.get_correction_patterns()).

    One decision per device. More than one labelled row for a logical key
    is an integrity violation: raises rather than guessing.
    """
    rows = cursor.execute("""
        SELECT o.id, o.device_id, o.human_decision, o.corrected_category, o.reviewed_at,
               (SELECT rc.id FROM review_cases rc
                WHERE rc.device_id = o.device_id AND rc.classifier_name = o.classifier_name
                  AND rc.hypothesis_category = o.hypothesis_category)
        FROM classification_observations o
        WHERE o.human_decision IS NOT NULL
          AND o.classifier_name = ? AND o.hypothesis_category = ?
        ORDER BY o.device_id, o.id
    """, (classifier_name, hypothesis_category)).fetchall()
    devices = [row[1] for row in rows]
    if len(devices) != len(set(devices)):
        raise RuntimeError(
            f"more than one labelled observation for a logical key of "
            f"{classifier_name}/{hypothesis_category}")
    return [
        Decision(labelled_observation_id=row[0], device_id=row[1], human_decision=row[2],
                 corrected_category=row[3], decided_at=row[4], review_case_id=row[5])
        for row in rows
        if row[4] is not None and _instant(row[4]) < _instant(before)
        and (device_id is None or row[1] == device_id)
        and (exclude_device_id is None or row[1] != exclude_device_id)
    ]


def annotation_exists(cursor, observation_id: str, memory_type: str) -> bool:
    return cursor.execute(
        "SELECT 1 FROM precedent_annotations WHERE observation_id = ? AND memory_type = ?",
        (observation_id, memory_type)).fetchone() is not None


def insert_annotation(cursor, observation: dict, annotation: Annotation, trigger: str,
                      created_at: str, policy_version: str) -> str:
    """Write one annotation and its evidence snapshot. Does not commit."""
    annotation_id = f"pan_{uuid4().hex[:12]}"
    cursor.execute("""
        INSERT INTO precedent_annotations (
            id, observation_id, memory_type, annotation_trigger,
            device_id, classifier_name, hypothesis_category,
            result, suggested_outcome, suggested_category,
            evidence_count, sample_size, correction_count, outcome_distribution,
            evidence_maturity, evidence_as_of, evidence_digest, policy_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (annotation_id, observation["id"], annotation.memory_type, trigger,
          observation["device_id"], observation["classifier_name"],
          observation["hypothesis_category"],
          annotation.result, annotation.suggested_outcome, annotation.suggested_category,
          annotation.evidence_count, annotation.sample_size, annotation.correction_count,
          json.dumps(annotation.outcome_distribution, sort_keys=True, ensure_ascii=False),
          annotation.evidence_maturity, annotation.evidence_as_of,
          annotation.evidence_digest, policy_version, created_at))
    for decision in annotation.evidence:
        cursor.execute("""
            INSERT INTO precedent_annotation_evidence (
                annotation_id, review_case_id, labelled_observation_id, device_id,
                human_decision, corrected_category, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (annotation_id, decision.review_case_id, decision.labelled_observation_id,
              decision.device_id, decision.human_decision, decision.corrected_category,
              decision.decided_at))
    return annotation_id


def insert_audit_failure(cursor, source: str, observation_id: Optional[str],
                         review_case_id: Optional[str], error: BaseException,
                         created_at: str, policy_version: str) -> None:
    cursor.execute("""
        INSERT INTO precedent_audit (
            id, event, source, observation_id, review_case_id,
            error_class, error_message, policy_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (f"pau_{uuid4().hex[:12]}", ANNOTATION_FAILED, source, observation_id,
          review_case_id, type(error).__name__, str(error)[:ERROR_MESSAGE_LIMIT],
          policy_version, created_at))
