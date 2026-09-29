"""M1: raw classifier hypothesis fields are immutable at the database level.

Value-change semantics: an UPDATE that sets a raw column to a DIFFERENT
value is aborted; assigning the same value (e.g. `SET x = x`) is allowed.
Review/workflow columns stay writable.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.storage import RAW_HYPOTHESIS_COLUMNS, Storage
from tests.test_storage import _obs, _resolved_case

# A different value of the right type for every protected column.
CHANGED_VALUE = {
    "observation_group_id": "g_other",
    "device_id": "dev_other",
    "classifier_name": "other_classifier",
    "hypothesis_category": "occupancy",
    "hypothesis_confidence": 0.123,
    "hypothesis_reasoning": "rewritten reasoning",
    "device_name": "Renamed",
    "device_model": "M2",
    "device_manufacturer": "OtherCo",
    "device_source_adapter": "other_adapter",
    "device_entity_count": 99,
    "created_at": "2000-01-01T00:00:00Z",
}

REVIEW_UPDATE = (
    "UPDATE classification_observations SET review_status = 'reviewed', "
    "human_decision = 'approved', reviewed_at = '2026-09-29T00:00:00Z', "
    "review_reason = 'ok', reviewed_by = 'human' WHERE id = ?"
)


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as tmp:
        s = Storage(Path(tmp) / "m1.db")
        yield s
        if s.connection:
            s.connection.close()


def _row(storage, obs_id):
    return dict(storage.connect().execute(
        "SELECT * FROM classification_observations WHERE id = ?", (obs_id,)
    ).fetchone())


def _triggers(storage):
    """Raw-hypothesis triggers only (the identity trigger is tested separately)."""
    raw = {f"trg_obs_immutable_{c}" for c in RAW_HYPOTHESIS_COLUMNS}
    return sorted(r[0] for r in storage.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'") if r[0] in raw)


def _all_triggers(storage):
    return sorted(r[0] for r in storage.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'"))


def test_exactly_twelve_protected_columns():
    assert len(RAW_HYPOTHESIS_COLUMNS) == 12
    assert set(RAW_HYPOTHESIS_COLUMNS) == set(CHANGED_VALUE)


@pytest.mark.parametrize("column", sorted(CHANGED_VALUE))
def test_changing_each_raw_column_is_rejected(storage, column):
    obs = _obs(storage, "g1", "dev1", "env", "environmental")
    before = _row(storage, obs)

    with pytest.raises(sqlite3.IntegrityError) as exc:
        storage.connect().execute(
            f"UPDATE classification_observations SET {column} = ? WHERE id = ?",
            (CHANGED_VALUE[column], obs),
        )
    storage.connect().rollback()

    # recognisable message naming the exact column
    assert str(exc.value) == (
        f"IMMUTABLE_RAW_HYPOTHESIS: classification_observations.{column} "
        "cannot be changed after insert"
    )
    assert _row(storage, obs) == before


def test_changing_several_raw_columns_at_once_is_rejected(storage):
    obs = _obs(storage, "g1", "dev1", "env", "environmental")
    before = _row(storage, obs)

    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_RAW_HYPOTHESIS"):
        storage.connect().execute(
            "UPDATE classification_observations SET hypothesis_category = ?, "
            "hypothesis_confidence = ?, created_at = ? WHERE id = ?",
            ("occupancy", 0.99, "2000-01-01T00:00:00Z", obs),
        )
    storage.connect().rollback()
    assert _row(storage, obs) == before


def test_mixed_review_and_raw_update_is_rejected_atomically(storage):
    obs = _obs(storage, "g1", "dev1", "env", "environmental")
    before = _row(storage, obs)

    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_RAW_HYPOTHESIS"):
        storage.connect().execute(
            "UPDATE classification_observations SET human_decision = 'approved', "
            "hypothesis_category = 'occupancy' WHERE id = ?", (obs,),
        )
    storage.connect().rollback()
    assert _row(storage, obs) == before  # review column not written either


def test_same_value_assignment_is_allowed(storage):
    obs = _obs(storage, "g1", "dev1", "env", "environmental")
    before = _row(storage, obs)
    conn = storage.connect()

    set_clause = ", ".join(f"{c} = {c}" for c in RAW_HYPOTHESIS_COLUMNS)
    conn.execute(f"UPDATE classification_observations SET {set_clause} WHERE id = ?", (obs,))
    conn.execute(
        "UPDATE classification_observations SET hypothesis_category = ? WHERE id = ?",
        (before["hypothesis_category"], obs),
    )
    conn.commit()
    assert _row(storage, obs) == before


@pytest.mark.parametrize("old, new", [(None, "now set"), ("was set", None)])
def test_null_transitions_count_as_changes(storage, old, new):
    obs = _obs(storage, "g1", "dev1", "env", "environmental")
    conn = storage.connect()
    conn.execute("DROP TRIGGER trg_obs_immutable_hypothesis_reasoning")
    conn.execute("UPDATE classification_observations SET hypothesis_reasoning = ? "
                 "WHERE id = ?", (old, obs))
    conn.commit()
    storage.connection.close()
    storage.connection = None
    storage.init_schema()  # re-creates the dropped trigger

    with pytest.raises(sqlite3.IntegrityError, match="hypothesis_reasoning"):
        storage.connect().execute(
            "UPDATE classification_observations SET hypothesis_reasoning = ? "
            "WHERE id = ?", (new, obs))
    storage.connect().rollback()


def test_review_only_update_is_allowed(storage):
    obs = _obs(storage, "g1", "dev1", "env", "environmental")
    conn = storage.connect()
    conn.execute(REVIEW_UPDATE, (obs,))
    conn.commit()
    row = _row(storage, obs)
    assert row["human_decision"] == "approved"
    assert row["hypothesis_category"] == "environmental"


def test_resolve_review_case_still_works(storage):
    case_id, obs = _resolved_case(storage, "corrected", "occupancy")
    row = _row(storage, obs)
    assert row["human_decision"] == "corrected"
    assert row["corrected_category"] == "occupancy"
    assert row["hypothesis_category"] == "environmental"  # raw guess preserved


def test_insert_of_new_observation_still_works(storage):
    first = _obs(storage, "g1", "dev1", "env", "environmental")
    second = _obs(storage, "g2", "dev1", "env", "environmental")
    assert first and second and first != second
    assert storage.connect().execute(
        "SELECT COUNT(*) FROM classification_observations").fetchone()[0] == 2


def test_schema_init_is_idempotent(storage):
    expected = sorted(f"trg_obs_immutable_{c}" for c in RAW_HYPOTHESIS_COLUMNS)
    assert _triggers(storage) == expected
    storage.init_schema()
    storage.init_schema()
    assert _triggers(storage) == expected


def test_adding_triggers_to_existing_database_changes_no_data():
    """Simulates the D1 upgrade: a pre-M1 database with history gains the
    triggers on startup, and not a single existing row changes."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pre_m1.db"
        s = Storage(path)
        _obs(s, "g1", "dev1", "env", "environmental")
        _resolved_case(s, "approved")
        for name in _all_triggers(s):
            s.connect().execute(f"DROP TRIGGER {name}")
        s.connect().commit()
        before = [line for line in s.connect().iterdump()
                  if line.startswith("INSERT")]
        s.connection.close()

        upgraded = Storage(path)  # startup on the pre-M1 database
        after = [line for line in upgraded.connect().iterdump()
                 if line.startswith("INSERT")]
        raw_triggers = _triggers(upgraded)
        all_triggers = _all_triggers(upgraded)
        upgraded.connection.close()

    assert after == before
    assert len(raw_triggers) == 12
    assert all_triggers == sorted(raw_triggers + ["trg_obs_immutable_id"])
