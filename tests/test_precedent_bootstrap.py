"""STEP 7a bootstrap: a case that was pending before the layer started is
annotated inside resolve_review_case(), immediately before its first human
decision - and can never stop, delay or alter that decision."""

import sqlite3

import pytest

from src import precedent_store
from src.storage import Storage
from tests.precedent_helpers import (
    PRECEDENT_TABLES, count, decided, pending, precedent_counts, rows, start_layer,
)

GROUPS = (("energy_meter", "energy", 7), ("environmental_sensor", "environmental", 6),
          ("motion_sensor", "motion", 5))


def _annotation(storage, observation_id):
    found = rows(storage, "precedent_annotations", where=f"WHERE observation_id = '{observation_id}'")
    return found[0] if found else None


def _case(storage, case_id):
    return rows(storage, "review_cases", where=f"WHERE id = '{case_id}'")[0]


def _obs(storage, observation_id):
    return rows(storage, "classification_observations", where=f"WHERE id = '{observation_id}'")[0]


@pytest.fixture
def legacy(tmp_path):
    """Before the layer: dev0 decided (approved), dev1..dev3 pending for
    the same (classifier, category). Then the layer starts."""
    path = tmp_path / "core.db"
    storage = Storage(path, precedent_mode="off")
    decided(storage, "dev0")
    cases = [pending(storage, f"dev{n}") for n in (1, 2, 3)]
    storage.connection.close()
    storage = start_layer(path)
    yield storage, cases
    storage.connection and storage.connection.close()


def test_eligible_case_is_annotated_just_before_its_decision(legacy):
    storage, ((case, obs), *_) = legacy
    assert storage.resolve_review_case(case, obs, "rejected") == "resolved"
    a = _annotation(storage, obs)
    assert (a["memory_type"], a["annotation_trigger"]) == ("class_pattern", "bootstrap_pending")
    assert (a["suggested_outcome"], a["evidence_count"], a["sample_size"]) == ("approved", 1, 1)
    row, resolved = _obs(storage, obs), _case(storage, case)
    assert (resolved["status"], resolved["decision"], row["human_decision"]) == (
        "resolved", "rejected", "rejected")
    assert a["created_at"] < resolved["decided_at"] == row["reviewed_at"]
    (evidence,) = rows(storage, "precedent_annotation_evidence")
    assert evidence["device_id"] == "dev0"


def test_the_decision_being_recorded_is_never_its_own_evidence(legacy):
    storage, cases = legacy
    for case, obs in cases:
        storage.resolve_review_case(case, obs, "corrected", corrected_category="occupancy")
    for annotation in rows(storage, "precedent_annotations"):
        evidence = rows(storage, "precedent_annotation_evidence",
                        where=f"WHERE annotation_id = '{annotation['id']}'")
        assert annotation["observation_id"] not in {e["labelled_observation_id"] for e in evidence}
        assert annotation["device_id"] not in {e["device_id"] for e in evidence}
        assert all(e["decided_at"] < annotation["created_at"] for e in evidence)


def test_resolving_in_sequence_grows_the_evidence(legacy):
    storage, cases = legacy
    for case, obs in cases:
        storage.resolve_review_case(case, obs, "rejected")
    sizes = [_annotation(storage, obs)["sample_size"] for _, obs in cases]
    assert sizes == [1, 2, 3]
    last = _annotation(storage, cases[-1][1])
    assert (last["suggested_outcome"], last["evidence_count"]) == ("rejected", 2)
    assert last["outcome_distribution"] == '{"approved": 1, "rejected": 2}'


def test_decision_timestamp_is_forced_after_the_annotation(legacy, monkeypatch):
    """Even with a clock that does not advance."""
    storage, ((case, obs), *_) = legacy
    import src.precedent_annotator as annotator
    import src.storage as storage_module
    frozen = "2099-01-01T00:00:00Z"
    monkeypatch.setattr(annotator, "_now", lambda: frozen)

    class Frozen(storage_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2099, 1, 1, tzinfo=tz)
    monkeypatch.setattr(storage_module, "datetime", Frozen)
    assert storage.resolve_review_case(case, obs, "approved") == "resolved"
    assert _annotation(storage, obs)["created_at"] == frozen
    assert _case(storage, case)["decided_at"] == "2099-01-01T00:00:00.000001Z"


def test_first_case_of_a_group_without_earlier_decisions_gets_no_annotation(tmp_path):
    path = tmp_path / "core.db"
    storage = Storage(path, precedent_mode="off")
    first, second = pending(storage, "dev1"), pending(storage, "dev2")
    storage.connection.close()
    storage = start_layer(path)
    storage.resolve_review_case(*first, "approved")
    storage.resolve_review_case(*second, "approved")
    assert _annotation(storage, first[1]) is None            # no evidence: not a failure
    assert _annotation(storage, second[1])["sample_size"] == 1
    assert count(storage, "precedent_audit", "WHERE event = 'annotation_failed'") == 0


def test_the_eighteen(tmp_path):
    """Shaped like production on 2026-10-03 (2 resolved, 18 pending; the
    split into groups is illustrative). Resolved one after another in a
    fixed order: evidence grows inside a (classifier, category) group,
    never across groups."""
    path = tmp_path / "core.db"
    storage = Storage(path, precedent_mode="off")
    decided(storage, "devA", classifier="environmental_sensor", category="environmental")
    decided(storage, "devB", classifier="environmental_sensor", category="environmental")
    todo = [(classifier, pending(storage, f"{classifier}_{n}", classifier, category))
            for classifier, category, size in GROUPS for n in range(size)]
    assert len(todo) == 18
    storage.connection.close()

    storage = start_layer(path)
    for _, (case, obs) in todo:
        assert storage.resolve_review_case(case, obs, "approved") == "resolved"
    sizes = {c: [(_annotation(storage, obs) or {"sample_size": None})["sample_size"]
                 for name, (_, obs) in todo if name == c] for c, _, _ in GROUPS}
    assert sizes == {"energy_meter": [None, 1, 2, 3, 4, 5, 6],
                     "environmental_sensor": [2, 3, 4, 5, 6, 7],
                     "motion_sensor": [None, 1, 2, 3, 4]}
    assert count(storage, "precedent_annotations") == 16
    assert count(storage, "review_cases", "WHERE status = 'pending'") == 0
    assert count(storage, "precedent_audit", "WHERE event = 'annotation_failed'") == 0
    assert {a["annotation_trigger"] for a in rows(storage, "precedent_annotations")} == {
        "bootstrap_pending"}


# --- human decision > precedent annotation ---------------------------------------

def _boom(*args, **kwargs):
    raise RuntimeError("precedent store is broken")


def _half_written(monkeypatch):
    original = precedent_store.insert_annotation

    def half(cursor, *args, **kwargs):
        original(cursor, *args, **kwargs)
        raise sqlite3.OperationalError("failed while writing evidence")
    monkeypatch.setattr(precedent_store, "insert_annotation", half)


@pytest.mark.parametrize("failure", ["computing", "writing", "half_written"])
def test_failing_annotator_never_blocks_the_decision(legacy, monkeypatch, failure):
    storage, ((case, obs), *_) = legacy
    if failure == "computing":
        monkeypatch.setattr(precedent_store, "labelled_decisions", _boom)
    elif failure == "writing":
        monkeypatch.setattr(precedent_store, "insert_annotation", _boom)
    else:
        _half_written(monkeypatch)

    assert storage.resolve_review_case(case, obs, "corrected",
                                       corrected_category="occupancy") == "resolved"
    assert (_case(storage, case)["status"], _obs(storage, obs)["human_decision"]) == (
        "resolved", "corrected")
    assert count(storage, "precedent_annotations") == 0
    assert count(storage, "precedent_annotation_evidence") == 0
    (failed,) = rows(storage, "precedent_audit", where="WHERE event = 'annotation_failed'")
    assert (failed["source"], failed["observation_id"], failed["review_case_id"]) == (
        "bootstrap_pending", obs, case)
    # and it is never repaired later
    assert storage.resolve_review_case(case, obs, "approved") == "already_resolved"
    assert count(storage, "precedent_annotations") == 0


def test_failing_audit_never_blocks_the_decision_either(legacy, monkeypatch):
    storage, ((case, obs), *_) = legacy
    monkeypatch.setattr(precedent_store, "insert_annotation", _boom)
    monkeypatch.setattr(precedent_store, "insert_audit_failure", _boom)
    assert storage.resolve_review_case(case, obs, "approved") == "resolved"
    assert _obs(storage, obs)["human_decision"] == "approved"
    assert precedent_counts(storage) == {"precedent_annotations": 0,
                                         "precedent_annotation_evidence": 0, "precedent_audit": 1}


def test_decision_failing_after_the_annotation_leaves_no_annotation(legacy):
    """The dual-write refuses (the row was labelled from outside): the whole
    transaction rolls back, including the savepoint's annotation."""
    storage, ((case, obs), *_) = legacy
    conn = storage.connect()
    conn.execute("UPDATE classification_observations SET human_decision = 'approved', "
                 "review_status = 'reviewed' WHERE id = ?", (obs,))
    conn.commit()
    with pytest.raises(RuntimeError):
        storage.resolve_review_case(case, obs, "rejected")
    assert _case(storage, case)["status"] == "pending"
    assert precedent_counts(storage)["precedent_annotations"] == 0


@pytest.mark.parametrize("call, expected", [
    (lambda s, case, obs: s.resolve_review_case(case, "obs_other", "approved"), "stale"),
    (lambda s, case, obs: s.resolve_review_case("case_none", obs, "approved"), "not_found"),
])
def test_refused_calls_write_nothing(legacy, call, expected):
    storage, ((case, obs), *_) = legacy
    assert call(storage, case, obs) == expected
    assert precedent_counts(storage)["precedent_annotations"] == 0
    assert _case(storage, case)["status"] == "pending"
    assert not storage.connect().in_transaction


# --- eligibility --------------------------------------------------------------------

def test_no_backfill_for_cases_resolved_before_the_layer(legacy):
    storage, _ = legacy
    (resolved,) = rows(storage, "review_cases", where="WHERE status = 'resolved'")
    assert count(storage, "precedent_annotations") == 0              # not at start
    assert storage.resolve_review_case(
        resolved["id"], resolved["last_observation_id"], "rejected") == "already_resolved"
    assert count(storage, "precedent_annotations") == 0              # not on a repeated call


def test_observation_written_after_the_cut_off_is_not_bootstrapped(legacy):
    """A post-layer observation that had no evidence when observed stays
    without an annotation: its cohort is the normal one."""
    storage, _ = legacy
    case, obs = pending(storage, "dev9", classifier="other", category="thing")
    decided(storage, "dev8", classifier="other", category="thing")   # evidence appears later
    assert storage.resolve_review_case(case, obs, "approved") == "resolved"
    assert _annotation(storage, obs) is None
    assert count(storage, "precedent_audit", "WHERE event = 'annotation_failed'") == 0


def test_layer_off_resolves_exactly_as_before_and_writes_nothing(tmp_path):
    path = tmp_path / "core.db"
    storage = Storage(path, precedent_mode="off")
    decided(storage, "dev0")
    case, obs = pending(storage, "dev1")
    assert storage.resolve_review_case(case, obs, "approved") == "resolved"
    assert all(count(storage, table) == 0 for table in PRECEDENT_TABLES)


def test_layer_switched_off_after_start_does_not_bootstrap(legacy):
    storage, ((case, obs), *_) = legacy
    storage.connection.close()
    off = Storage(storage.db_path, precedent_mode="off")
    assert off.resolve_review_case(case, obs, "approved") == "resolved"
    assert count(off, "precedent_annotations") == 0
    assert [r["event"] for r in rows(off, "precedent_audit")] == ["layer_started"]
    off.connection.close()


def test_lost_transaction_is_raised_and_the_case_stays_pending(legacy, monkeypatch):
    """If SQLite dropped the transaction, no decision may be written on
    what is left of it: the resolver raises and the reviewer retries."""
    storage, ((case, obs), *_) = legacy

    def lose_transaction(cursor, *args, **kwargs):
        cursor.connection.rollback()
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(precedent_store, "insert_annotation", lose_transaction)
    with pytest.raises(sqlite3.OperationalError):
        storage.resolve_review_case(case, obs, "approved")
    assert _case(storage, case)["status"] == "pending"
    assert _obs(storage, obs)["human_decision"] is None
    monkeypatch.undo()
    assert storage.resolve_review_case(case, obs, "approved") == "resolved"
