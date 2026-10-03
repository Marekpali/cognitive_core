"""STEP 7a normal path: annotations written with the observation, inside the
sweep's transaction, without any effect on what STEP 7P does."""

import copy
import sqlite3

import pytest

from src import precedent_store
from src.storage import Storage
from tests.ha_payloads import full_home
from tests.precedent_helpers import (
    STEP7P_TABLES, count, precedent_counts, rows, start_layer, sweep,
)

MOTION = "motion_sensor"


def _renamed(*indices):
    devices, entities, states = copy.deepcopy(full_home())
    for index in indices:
        devices[index]["name"] = f"Renamed {index}"
    return devices, entities, states


def _case(storage, device_id, classifier):
    (case,) = rows(storage, "review_cases", where=(
        f"WHERE device_id = '{device_id}' AND classifier_name = '{classifier}'"))
    return case


@pytest.fixture
def home(tmp_path):
    """Active home (dev_motion, dev_multi, dev_socket classified) observed
    before the layer existed, with one human decision: dev_motion's motion
    hypothesis approved. Then the layer starts."""
    path = tmp_path / "core.db"
    storage = Storage(path, precedent_mode="off")
    sweep(storage)
    case = _case(storage, "dev_motion", MOTION)
    assert storage.resolve_review_case(case["id"], case["last_observation_id"],
                                       "approved") == "resolved"
    storage.connection.close()
    storage = start_layer(path)
    yield storage
    storage.connection and storage.connection.close()


def test_unchanged_devices_get_no_annotation(home):
    result = sweep(home, source="entity_registry_updated")
    assert result.counts["unchanged"] == 4 and result.observations_written == 0
    assert precedent_counts(home) == {"precedent_annotations": 0,
                                      "precedent_annotation_evidence": 0, "precedent_audit": 1}


def test_class_pattern_for_a_new_observation_uses_other_devices_only(home):
    sweep(home, _renamed(1), "device_registry_updated")      # dev_multi: env + motion
    annotations = rows(home, "precedent_annotations")
    assert [(a["device_id"], a["classifier_name"], a["memory_type"], a["annotation_trigger"])
            for a in annotations] == [("dev_multi", MOTION, "class_pattern", "observation")]
    (a,) = annotations
    assert (a["result"], a["suggested_outcome"], a["evidence_count"], a["sample_size"]) == (
        "suggestion", "approved", 1, 1)
    assert a["evidence_maturity"] == "insufficient" and a["policy_version"] == "7a.1"
    (evidence,) = rows(home, "precedent_annotation_evidence")
    assert evidence["device_id"] == "dev_motion" and evidence["human_decision"] == "approved"
    assert evidence["review_case_id"] == _case(home, "dev_motion", MOTION)["id"]
    # the annotated observation is the new one, linked to its input
    (obs,) = rows(home, "classification_observations", where=f"WHERE id = '{a['observation_id']}'")
    assert obs["input_id"] and obs["human_decision"] is None
    assert a["evidence_as_of"] == evidence["decided_at"] < a["created_at"]


def test_device_precedent_recalls_the_decision_and_is_kept_separate(home):
    sweep(home, _renamed(0), "device_registry_updated")      # dev_motion itself
    (a,) = rows(home, "precedent_annotations")
    assert (a["device_id"], a["memory_type"], a["suggested_outcome"]) == (
        "dev_motion", "device_precedent", "approved")
    # leave-one-device-out: its own decision is not class-pattern evidence
    assert count(home, "precedent_annotations", "WHERE memory_type = 'class_pattern'") == 0
    assert _case(home, "dev_motion", MOTION)["status"] == "resolved"


def test_no_evidence_writes_nothing_and_is_not_a_failure(home):
    sweep(home, _renamed(2), "device_registry_updated")      # dev_socket: energy only
    assert precedent_counts(home)["precedent_annotations"] == 0
    assert [r["event"] for r in rows(home, "precedent_audit")] == ["layer_started"]


def test_existing_annotation_is_never_refreshed(home):
    sweep(home, _renamed(1), "device_registry_updated")
    before = rows(home, "precedent_annotations") + rows(home, "precedent_annotation_evidence")
    # more evidence arrives: dev_multi's own motion case is decided, and a
    # further sweep runs
    case = _case(home, "dev_multi", MOTION)
    assert home.resolve_review_case(case["id"], case["last_observation_id"],
                                    "corrected", corrected_category="occupancy") == "resolved"
    sweep(home, _renamed(1), "entity_registry_updated")
    assert rows(home, "precedent_annotations") + rows(home, "precedent_annotation_evidence") \
        == before


def test_decision_made_after_the_annotation_is_not_in_its_evidence(home):
    sweep(home, _renamed(1), "device_registry_updated")
    (a,) = rows(home, "precedent_annotations")
    case = _case(home, "dev_multi", MOTION)
    home.resolve_review_case(case["id"], case["last_observation_id"], "rejected")
    (evidence,) = rows(home, "precedent_annotation_evidence")
    assert evidence["labelled_observation_id"] != a["observation_id"]
    (obs,) = rows(home, "classification_observations", where=f"WHERE id = '{a['observation_id']}'")
    assert obs["reviewed_at"] > a["created_at"]


def _step7p(storage) -> dict:
    return {table: rows(storage, table) for table in STEP7P_TABLES}


def _blank(tables: dict) -> str:
    """Rows without the random ids and timestamps."""
    import json
    from tests.equivalence_scenario import normalise
    return normalise(tables)


def test_layer_on_and_off_write_identical_step_7p_rows(tmp_path):
    results = {}
    for mode in ("off", "shadow"):
        path = tmp_path / f"{mode}.db"
        storage = Storage(path, precedent_mode="off")
        sweep(storage)
        case = _case(storage, "dev_motion", MOTION)
        storage.resolve_review_case(case["id"], case["last_observation_id"], "approved")
        storage.connection.close()
        storage = start_layer(path) if mode == "shadow" else Storage(path, precedent_mode="off")
        outcomes = [sweep(storage, _renamed(0, 1, 2), "device_registry_updated").devices,
                    sweep(storage, _renamed(0, 1, 2), "entity_registry_updated").devices]
        results[mode] = _blank({"outcomes": outcomes, "rows": _step7p(storage)})
        annotations = count(storage, "precedent_annotations")
        storage.connection.close()
        assert (annotations > 0) is (mode == "shadow")
    assert results["off"] == results["shadow"]


def test_observation_mode_shadow_never_annotates(home):
    result = sweep(home, _renamed(0, 1, 2), "device_registry_updated", mode="shadow")
    assert result.counts["classified"] == 3 and result.observations_written == 0
    assert precedent_counts(home)["precedent_annotations"] == 0


# --- failure isolation ----------------------------------------------------------

def _boom(*args, **kwargs):
    raise RuntimeError("precedent store is broken")


@pytest.mark.parametrize("target, failures_expected", [
    ("labelled_decisions", 2),      # reading evidence fails for both new observations
    ("insert_annotation", 1),       # only the one that has evidence tries to write
])
def test_failing_annotator_leaves_the_sweep_untouched_and_is_audited(
        home, monkeypatch, target, failures_expected):
    monkeypatch.setattr(precedent_store, target, _boom)
    result = sweep(home, _renamed(1), "device_registry_updated")
    assert result.counts["classified"] == 1 and result.counts["error"] == 0
    assert result.devices["dev_multi"]["outcome"] == "classified"
    assert result.observations_written == 2
    assert count(home, "classification_inputs") == 5 and count(home, "precedent_annotations") == 0
    assert count(home, "precedent_annotation_evidence") == 0
    failures = rows(home, "precedent_audit", where="WHERE event = 'annotation_failed'")
    assert [(f["source"], f["error_class"], f["review_case_id"]) for f in failures] == [
        ("observation", "RuntimeError", None)] * failures_expected
    new = {o["id"] for o in rows(home, "classification_observations",
                                 where="WHERE device_id = 'dev_multi' AND input_id IS NOT NULL")}
    assert {f["observation_id"] for f in failures} <= new


def test_partial_annotation_is_rolled_back(home, monkeypatch):
    """Annotation row written, then the evidence insert fails: nothing of
    the annotation remains."""
    original = precedent_store.insert_annotation

    def half(cursor, *args, **kwargs):
        original(cursor, *args, **kwargs)
        raise sqlite3.OperationalError("disk full while writing evidence")
    monkeypatch.setattr(precedent_store, "insert_annotation", half)
    result = sweep(home, _renamed(1), "device_registry_updated")
    assert result.counts["error"] == 0
    assert count(home, "precedent_annotations") == 0
    assert count(home, "precedent_annotation_evidence") == 0
    assert count(home, "precedent_audit", "WHERE event = 'annotation_failed'") == 1


def test_failing_audit_cannot_roll_back_the_pipeline(home, monkeypatch):
    """The audit mechanism has no authority either: annotation fails AND its
    audit fails -> input, observations and review cases are still committed."""
    monkeypatch.setattr(precedent_store, "insert_annotation", _boom)
    monkeypatch.setattr(precedent_store, "insert_audit_failure", _boom)
    before = count(home, "classification_observations")
    result = sweep(home, _renamed(1), "device_registry_updated")
    assert result.counts["classified"] == 1 and result.counts["error"] == 0
    assert count(home, "classification_observations") == before + 2
    assert count(home, "classification_inputs") == 5
    assert _case(home, "dev_multi", MOTION)["last_observation_id"] in {
        o["id"] for o in rows(home, "classification_observations", where="WHERE input_id IS NOT NULL")}
    assert precedent_counts(home) == {"precedent_annotations": 0,
                                      "precedent_annotation_evidence": 0, "precedent_audit": 1}
    # and the gate advanced: the next sweep sees the device as unchanged
    assert sweep(home, _renamed(1), "entity_registry_updated").counts["unchanged"] == 4


def test_integrity_violation_in_the_evidence_is_a_failure_not_a_guess(home):
    """Two labelled rows for one logical key (only possible from outside
    the code): the annotator refuses to guess, the sweep is unaffected."""
    conn = home.connect()
    conn.execute("INSERT INTO classification_observations (id, observation_group_id, device_id, "
                 "classifier_name, hypothesis_category, hypothesis_confidence, "
                 "hypothesis_reasoning, device_name, device_model, device_manufacturer, "
                 "device_source_adapter, device_entity_count, created_at, review_status, "
                 "human_decision, reviewed_at) SELECT 'obs_dup', observation_group_id, device_id, "
                 "classifier_name, hypothesis_category, 0.5, '', '', '', '', 'ha', 1, created_at, "
                 "'reviewed', 'rejected', reviewed_at FROM classification_observations "
                 "WHERE human_decision IS NOT NULL")
    conn.commit()
    result = sweep(home, _renamed(1), "device_registry_updated")
    assert result.counts["error"] == 0 and count(home, "precedent_annotations") == 0
    assert {f["error_class"] for f in rows(
        home, "precedent_audit", where="WHERE event = 'annotation_failed'")} == {"RuntimeError"}


# --- one transaction ---------------------------------------------------------------

def test_crash_before_commit_leaves_nothing_of_the_device(home, monkeypatch):
    """Input, observations, review case and annotation commit together: a
    process dying inside the transaction leaves none of them."""
    path = home.db_path
    before = {t: len(rows(path, t)) for t in STEP7P_TABLES + ("precedent_annotations",)}

    def die(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(precedent_store, "insert_annotation", die)
    with pytest.raises(KeyboardInterrupt):
        sweep(home, _renamed(1), "device_registry_updated")
    home.connection.close()         # the process is gone; nothing was committed
    home.connection = None
    assert {t: len(rows(path, t)) for t in before} == before


def test_after_commit_every_new_observation_is_accounted_for(home, monkeypatch):
    original = precedent_store.insert_annotation
    calls = []

    def flaky(cursor, observation, *args, **kwargs):
        calls.append(observation["id"])
        if len(calls) == 1:
            raise RuntimeError("first annotation fails")
        return original(cursor, observation, *args, **kwargs)
    monkeypatch.setattr(precedent_store, "insert_annotation", flaky)
    sweep(home, _renamed(0, 1), "device_registry_updated")
    annotated = {a["observation_id"] for a in rows(home, "precedent_annotations")}
    failed = {f["observation_id"] for f in rows(
        home, "precedent_audit", where="WHERE event = 'annotation_failed'")}
    assert len(annotated) == 1 and len(failed) == 1 and not annotated & failed
    # the remaining new observation (dev_multi / environmental) had no evidence
    new = {o["id"] for o in rows(home, "classification_observations",
                                 where="WHERE input_id IS NOT NULL AND created_at > "
                                       "(SELECT created_at FROM precedent_audit "
                                       "WHERE event = 'layer_started')")}
    assert len(new - annotated - failed) == 1


# --- review-case isolation (at least as good as STEP 7P) -----------------------------

def test_one_failing_review_case_does_not_roll_back_anything_else(home, monkeypatch):
    original = Storage._upsert_review_case

    def failing(cursor, device_id, classifier_name, hypothesis_category, observation_id):
        if classifier_name == "environmental_sensor":
            raise sqlite3.OperationalError("review_cases is locked")
        return original(cursor, device_id, classifier_name, hypothesis_category, observation_id)
    monkeypatch.setattr(Storage, "_upsert_review_case", staticmethod(failing))
    env_before = _case(home, "dev_multi", "environmental_sensor")["last_observation_id"]

    result = sweep(home, _renamed(1), "device_registry_updated")
    assert result.devices["dev_multi"]["outcome"] == "classified" and result.counts["error"] == 0
    new = [o for o in rows(home, "classification_observations", where="WHERE device_id = 'dev_multi'")
           if o["id"] in {r["id"] for r in rows(home, "classification_observations")[-2:]}]
    assert len(new) == 2 and count(home, "classification_inputs") == 5
    # the other case WAS refreshed, the failing one was not
    assert _case(home, "dev_multi", MOTION)["last_observation_id"] in {o["id"] for o in new}
    assert _case(home, "dev_multi", "environmental_sensor")["last_observation_id"] == env_before
    assert count(home, "precedent_annotations") == 1

    monkeypatch.undo()
    home.connection.close()
    reopened = Storage(home.db_path, precedent_mode="shadow")   # start reconciles it
    assert _case(reopened, "dev_multi", "environmental_sensor")["last_observation_id"] in {
        o["id"] for o in new}
    reopened.connection.close()


def test_lost_transaction_fails_loudly_instead_of_committing_the_rest(home, monkeypatch):
    """An error that makes SQLite drop the whole transaction (disk full,
    I/O error) cannot be isolated by a savepoint: the sweep must report an
    error, not return ids of rows that no longer exist."""
    def lose_transaction(cursor, *args, **kwargs):
        cursor.connection.rollback()
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(precedent_store, "insert_annotation", lose_transaction)
    before = {table: count(home, table) for table in STEP7P_TABLES}
    result = sweep(home, _renamed(1), "device_registry_updated")
    assert result.counts["error"] == 1 and result.observations_written == 0
    after = {table: count(home, table) for table in STEP7P_TABLES}
    assert {**after, "classification_sweeps": before["classification_sweeps"]} == before
    assert not home.connect().in_transaction
