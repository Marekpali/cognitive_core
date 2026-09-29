"""M1: observation identity (classification_observations.id) is immutable.

Kept separate from RAW_HYPOTHESIS_COLUMNS: `id` is record identity, not
classifier output. Model: record identity immutable, raw hypothesis
immutable, review metadata mutable.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.storage import (
    OBSERVATION_IDENTITY_TRIGGER,
    RAW_HYPOTHESIS_COLUMNS,
    Storage,
)
from tests.test_storage import _obs

MESSAGE = ("IMMUTABLE_OBSERVATION_IDENTITY: classification_observations.id "
           "cannot be changed after insert")


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as tmp:
        s = Storage(Path(tmp) / "id.db")
        yield s
        if s.connection:
            s.connection.close()


def _row(storage, obs_id):
    row = storage.connect().execute(
        "SELECT * FROM classification_observations WHERE id = ?", (obs_id,)
    ).fetchone()
    return dict(row) if row else None


def test_identity_is_not_a_raw_hypothesis_column():
    assert "id" not in RAW_HYPOTHESIS_COLUMNS
    assert OBSERVATION_IDENTITY_TRIGGER == "trg_obs_immutable_id"


def test_changing_id_is_rejected_and_row_is_identical(storage):
    obs = _obs(storage, "g1", "dev1", "env", "environmental")
    before = _row(storage, obs)

    with pytest.raises(sqlite3.IntegrityError) as exc:
        storage.connect().execute(
            "UPDATE classification_observations SET id = 'obs_forged' WHERE id = ?",
            (obs,),
        )
    storage.connect().rollback()

    assert str(exc.value) == MESSAGE
    assert _row(storage, obs) == before
    assert _row(storage, "obs_forged") is None


def test_same_id_assignment_is_allowed(storage):
    obs = _obs(storage, "g1", "dev1", "env", "environmental")
    before = _row(storage, obs)
    conn = storage.connect()
    conn.execute("UPDATE classification_observations SET id = id WHERE id = ?", (obs,))
    conn.commit()
    assert _row(storage, obs) == before


def test_review_case_pointer_stays_valid_after_refused_id_change(storage):
    obs = _obs(storage, "g1", "dev1", "env", "environmental")
    case_id = storage.upsert_review_case("dev1", "env", "environmental", obs)

    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_OBSERVATION_IDENTITY"):
        storage.connect().execute(
            "UPDATE classification_observations SET id = 'obs_forged' WHERE id = ?",
            (obs,),
        )
    storage.connect().rollback()

    pointer = storage.connect().execute(
        "SELECT co.id FROM review_cases rc JOIN classification_observations co "
        "ON co.id = rc.last_observation_id WHERE rc.id = ?", (case_id,),
    ).fetchone()
    assert pointer is not None and pointer[0] == obs
    assert storage.resolve_review_case(
        case_id, expected_observation_id=obs, decision="approved") == "resolved"


def test_identity_trigger_created_idempotently(storage):
    def count():
        return storage.connect().execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
            "AND name = ?", (OBSERVATION_IDENTITY_TRIGGER,)).fetchone()[0]
    assert count() == 1
    storage.init_schema()
    assert count() == 1
