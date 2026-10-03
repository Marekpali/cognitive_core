"""Shared fixtures for the STEP 7a tests."""

import asyncio
import sqlite3
from pathlib import Path

from src.storage import Storage
from src.sweep import ObservationSweep
from tests.ha_payloads import full_home
from tests.test_storage import _obs
from tests.test_sweep import _classifiers

PRECEDENT_TABLES = ("precedent_annotations", "precedent_annotation_evidence", "precedent_audit")
STEP7P_TABLES = ("classification_inputs", "classification_observations", "review_cases",
                 "classification_sweeps")


def pending(storage, device_id, classifier="env", category="environmental", group=None):
    """One observation that is the current evidence of a pending case."""
    obs = _obs(storage, group or f"g_{device_id}", device_id, classifier, category)
    case = storage.upsert_review_case(device_id, classifier, category, obs)
    return case, obs


def decided(storage, device_id, decision="approved", corrected=None,
            classifier="env", category="environmental"):
    case, obs = pending(storage, device_id, classifier, category)
    assert storage.resolve_review_case(case, obs, decision,
                                       corrected_category=corrected) == "resolved"
    return case, obs


def start_layer(path: Path) -> Storage:
    """Reopen the database with the layer in shadow and started, as Core
    does on its first start in that mode."""
    storage = Storage(path, precedent_mode="shadow")
    storage.ensure_precedent_layer_started()
    return storage


def sweep(storage, snapshot=None, source="startup", mode="active", classifiers=None):
    return asyncio.run(ObservationSweep(storage, classifiers or _classifiers(), mode).run(
        source, *(snapshot or full_home())))


def rows(storage_or_path, table, columns="*", where="") -> list:
    conn = (storage_or_path.connect() if isinstance(storage_or_path, Storage)
            else sqlite3.connect(storage_or_path))
    cursor = conn.execute(f"SELECT {columns} FROM {table} {where} ORDER BY rowid")
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def count(storage, table, where="") -> int:
    return storage.connect().execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]


def precedent_counts(storage) -> dict:
    return {table: count(storage, table) for table in PRECEDENT_TABLES}
