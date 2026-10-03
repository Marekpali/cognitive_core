"""STEP 7a schema: tables, CHECKs, append-only triggers, the layer cut-off,
and the migration of a STEP 7P-era database."""

import sqlite3

import pytest

from src import precedent_store
from src.coverage_store import STEP7P_TRIGGERS
from src.learning.precedent import POLICY_VERSION, Decision, evaluate_class_pattern
from src.precedent_store import PRECEDENT_TABLES, PRECEDENT_TRIGGERS
from src.storage import Storage
from tests.precedent_helpers import count, decided, pending, rows, start_layer, sweep

OBSERVATION = {"id": "obs_x", "device_id": "dev_x", "classifier_name": "env",
               "hypothesis_category": "environmental"}
AUDIT = ("INSERT INTO precedent_audit (id, event, source, policy_version, created_at) "
         "VALUES (?, ?, ?, '7a.1', 't')")


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "core.db")
    yield s
    s.connection and s.connection.close()


def _annotation(storage) -> str:
    annotation = evaluate_class_pattern([Decision("obs_1", "dev_1", "approved", None, "t0", "c1")])
    cursor = storage.connect().cursor()
    annotation_id = precedent_store.insert_annotation(
        cursor, OBSERVATION, annotation, "observation", "t1", POLICY_VERSION)
    storage.connection.commit()
    return annotation_id


def test_schema_is_created_whatever_the_option_says(storage):
    names = {r[0] for r in storage.connect().execute("SELECT name FROM sqlite_master")}
    assert set(PRECEDENT_TABLES) <= names and set(PRECEDENT_TRIGGERS) <= names
    assert "idx_precedent_layer_started" in names
    assert storage.precedent_mode == "off"
    assert len(PRECEDENT_TRIGGERS) == 6
    triggers = {r[0] for r in storage.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert len(triggers) == 13 + len(STEP7P_TRIGGERS) + 6 == 24


@pytest.mark.parametrize("table", PRECEDENT_TABLES)
def test_tables_are_append_only_with_exact_messages(storage, table):
    annotation_id = _annotation(storage)
    conn = storage.connect()
    conn.execute(AUDIT, ("pau_1", "annotation_failed", "observation"))
    conn.commit()
    for op, sql in (("updated", f"UPDATE {table} SET rowid = rowid"),
                    ("deleted", f"DELETE FROM {table}")):
        with pytest.raises(sqlite3.IntegrityError) as exc:
            conn.execute(sql)
        assert str(exc.value) == f"APPEND_ONLY: {table} rows cannot be {op}"
        conn.rollback()
    assert count(storage, "precedent_annotations") == 1
    assert rows(storage, "precedent_annotation_evidence")[0]["annotation_id"] == annotation_id


@pytest.mark.parametrize("event, source", [
    ("layer_started", "observation"),        # the cut-off has no source
    ("annotation_failed", None),             # a failure must say where it happened
    ("annotation_failed", "review"),
    ("something_else", None),
])
def test_audit_check_refuses_inconsistent_rows(storage, event, source):
    with pytest.raises(sqlite3.IntegrityError):
        storage.connect().execute(AUDIT, ("pau_x", event, source))


@pytest.mark.parametrize("column, value", [("memory_type", "combined"),
                                           ("annotation_trigger", "annotation_failed"),
                                           ("result", "guess")])
def test_annotation_checks(storage, column, value):
    _annotation(storage)
    columns = [r[1] for r in storage.connect().execute("PRAGMA table_info(precedent_annotations)")]
    row = dict(rows(storage, "precedent_annotations")[0], id="pan_other",
               observation_id="obs_other", **{column: value})
    with pytest.raises(sqlite3.IntegrityError):
        storage.connect().execute(
            f"INSERT INTO precedent_annotations VALUES ({', '.join('?' for _ in columns)})",
            [row[c] for c in columns])


def test_one_annotation_per_observation_and_memory_type(storage):
    _annotation(storage)
    with pytest.raises(sqlite3.IntegrityError):
        _annotation(storage)


# --- the layer cut-off -------------------------------------------------------

def test_cut_off_is_never_written_while_off(tmp_path):
    storage = Storage(tmp_path / "core.db", precedent_mode="off")
    case, obs = pending(storage, "dev1")
    assert storage.resolve_review_case(case, obs, "approved") == "resolved"
    sweep(storage)
    assert rows(storage, "precedent_audit") == []
    assert precedent_store.layer_started_at(storage.connect()) is None


def test_cut_off_is_written_once_and_survives_restart_and_off(tmp_path):
    path = tmp_path / "core.db"
    Storage(path).connection
    first = start_layer(path)
    started_at = precedent_store.layer_started_at(first.connect())
    assert started_at and first.ensure_precedent_layer_started() == started_at
    first.connection.close()

    again = start_layer(path)                       # restart in shadow
    assert again.ensure_precedent_layer_started() == started_at
    again.connection.close()

    off = Storage(path, precedent_mode="off")       # temporary return to off
    assert precedent_store.layer_started_at(off.connect()) == started_at
    assert off._precedent_layer_started_at(off.connect()) is None
    assert [r["event"] for r in rows(off, "precedent_audit")] == ["layer_started"]
    with pytest.raises(sqlite3.IntegrityError):     # no second cut-off, ever
        off.connect().execute(AUDIT, ("pau_2", "layer_started", None))


def test_shadow_without_a_cut_off_is_not_started(tmp_path):
    """The resolver never writes the cut-off: in shadow before Core's first
    start the layer simply has not started."""
    storage = Storage(tmp_path / "core.db", precedent_mode="shadow")
    decided(storage, "dev0")
    case, obs = pending(storage, "dev1")
    assert storage.resolve_review_case(case, obs, "approved") == "resolved"
    assert rows(storage, "precedent_audit") == [] and count(storage, "precedent_annotations") == 0


# --- migration ----------------------------------------------------------------

def _step7p_era(path):
    original = Storage.init_step7a_schema
    Storage.init_step7a_schema = lambda self, cursor: None
    try:
        storage = Storage(path)
        sweep(storage)
        decided(storage, "dev1")
        storage.connection.close()
    finally:
        Storage.init_step7a_schema = original


def test_migrating_a_step_7p_database_changes_no_existing_row(tmp_path):
    path = tmp_path / "core.db"
    _step7p_era(path)
    conn = sqlite3.connect(path)
    before = [line for line in conn.iterdump() if line.startswith("INSERT")]
    assert not {r[0] for r in conn.execute("SELECT name FROM sqlite_master")} & set(PRECEDENT_TABLES)
    conn.close()

    for _ in range(2):                              # idempotent
        Storage(path).connect().close()
    conn = sqlite3.connect(path)
    assert [line for line in conn.iterdump() if line.startswith("INSERT")] == before
    assert set(PRECEDENT_TABLES) <= {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    conn.close()


def test_step_7p_code_runs_on_the_migrated_database(tmp_path):
    """Rollback proof: with the 7a schema present but the 7a code path
    absent (the 7P write methods), everything still works and nothing is
    written to the precedent tables."""
    path = tmp_path / "core.db"
    Storage(path).connect().close()                 # migrated
    storage = Storage(path)
    input_row = {"id": "inp_1", "device_id": "dev1", "fingerprint": "f",
                 "classifier_set_version": "1", "snapshot_json": "{}", "outcome": "classified",
                 "matched_classifiers": ["env"], "sweep_id": "swp_1", "created_at": "t"}
    obs = {"observation_group_id": "g", "device_id": "dev1", "classifier_name": "env",
           "hypothesis_category": "environmental", "hypothesis_confidence": 0.8,
           "hypothesis_reasoning": "", "device_name": "D", "device_model": "",
           "device_manufacturer": "", "device_source_adapter": "ha", "device_entity_count": 1}
    (observation_id,) = storage.record_classification_input(input_row, [obs])   # 7P path
    case = storage.upsert_review_case("dev1", "env", "environmental", observation_id)
    assert storage.resolve_review_case(case, observation_id, "approved") == "resolved"
    assert all(count(storage, table) == 0 for table in PRECEDENT_TABLES)
