# tests/test_storage.py

import pytest
from pathlib import Path
import tempfile
from datetime import datetime, timezone

from src.storage import Storage

@pytest.fixture
def temp_storage():
    """Create temporary storage.

    Explicitly closes the cached sqlite3 connection before
    TemporaryDirectory's __exit__ tries to delete the underlying file.
    Windows (unlike Unix) refuses to remove a file still held open by a
    live handle - without this, every test using temp_storage raises
    PermissionError [WinError 32] at teardown, even though the test
    body itself passes. Same fix already applied manually in
    test_thread_safety.py and test_ha_sensor.py for the same reason;
    this closes the gap for test_storage.py's own fixture instead of
    requiring every test function to remember it individually.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = Storage(Path(tmpdir) / "test.db")
        try:
            yield storage
        finally:
            if storage.connection:
                storage.connection.close()

def test_save_and_load_asset(temp_storage):
    """Test save/load"""

    asset = {
        'id': 'test_asset_1',
        'name': 'Test Device',
        'source_device_id': 'ha_123',
        'source_adapter': 'mock',
        'hypothesis': {
            'category': 'energy_meter',
            'confidence': 0.95,
            'reasoning': 'Test'
        },
        'lifecycle_state': 'provisional',
        'lifecycle_discovered_at': datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    }

    asset_id = temp_storage.save_asset(asset)
    assert asset_id == 'test_asset_1'

    loaded = temp_storage.load_asset(asset_id)
    assert loaded is not None
    assert loaded['name'] == 'Test Device'

# ---------------------------------------------------------------------------
# STEP 5: review_cases tests
# ---------------------------------------------------------------------------

def _obs(temp_storage, group, device_id, classifier_name, category,
         confidence=0.8, name="D1"):
    return temp_storage.log_classification_observation({
        "observation_group_id": group, "device_id": device_id,
        "classifier_name": classifier_name, "hypothesis_category": category,
        "hypothesis_confidence": confidence, "hypothesis_reasoning": "",
        "device_name": name, "device_model": "", "device_manufacturer": "",
        "device_source_adapter": "ha", "device_entity_count": 1,
    })


def _obs_at(temp_storage, obs_id, device_id, classifier_name, category, created_at):
    """Insert an observation with an explicit created_at (test fixture)."""
    conn = temp_storage.connect()
    conn.execute(
        "INSERT INTO classification_observations (id, observation_group_id, "
        "device_id, classifier_name, hypothesis_category, hypothesis_confidence, "
        "hypothesis_reasoning, device_name, device_model, device_manufacturer, "
        "device_source_adapter, device_entity_count, created_at) "
        "VALUES (?, ?, ?, ?, ?, 0.8, '', 'D1', '', '', 'ha', 1, ?)",
        (obs_id, f"g_{obs_id}", device_id, classifier_name, category, created_at),
    )
    conn.commit()
    return obs_id


def test_upsert_review_case_creates_pending(temp_storage):
    obs_id = _obs(temp_storage, "g1", "dev1", "motion_sensor", "motion")
    temp_storage.upsert_review_case("dev1", "motion_sensor", "motion", obs_id)
    assert temp_storage.count_pending_reviews() == 1


def test_repeated_identical_hypothesis_does_not_reopen_resolved(temp_storage):
    obs1 = _obs(temp_storage, "g1", "dev1", "motion_sensor", "motion")
    case_id = temp_storage.upsert_review_case("dev1", "motion_sensor", "motion", obs1)
    result = temp_storage.resolve_review_case(
        case_id, expected_observation_id=obs1, decision="approved"
    )
    assert result == "resolved"
    assert temp_storage.count_pending_reviews() == 0

    obs2 = _obs(temp_storage, "g2", "dev1", "motion_sensor", "motion")
    temp_storage.upsert_review_case("dev1", "motion_sensor", "motion", obs2)
    assert temp_storage.count_pending_reviews() == 0  # still resolved


def test_changed_category_creates_new_pending_case(temp_storage):
    obs1 = _obs(temp_storage, "g1", "dev1", "env", "environmental")
    case1 = temp_storage.upsert_review_case("dev1", "env", "environmental", obs1)
    temp_storage.resolve_review_case(case1, expected_observation_id=obs1, decision="approved")

    obs2 = _obs(temp_storage, "g2", "dev1", "env", "occupancy")
    temp_storage.upsert_review_case("dev1", "env", "occupancy", obs2)
    assert temp_storage.count_pending_reviews() == 1  # new, independent case


def test_multiple_classifiers_remain_separate_cases(temp_storage):
    for clf in ("motion_sensor", "environmental_sensor"):
        obs = _obs(temp_storage, "g1", "dev1", clf, clf)
        temp_storage.upsert_review_case("dev1", clf, clf, obs)
    assert temp_storage.count_pending_reviews() == 2


def test_resolve_dual_writes_only_last_observation(temp_storage):
    obs1 = _obs(temp_storage, "g1", "dev1", "motion_sensor", "motion")
    case_id = temp_storage.upsert_review_case("dev1", "motion_sensor", "motion", obs1)
    temp_storage.resolve_review_case(case_id, expected_observation_id=obs1, decision="approved")

    # obs2 shares the same logical key but was never itself reviewed -
    # dual-write must not have touched it.
    _obs(temp_storage, "g2", "dev1", "motion_sensor", "motion")

    analytics = temp_storage.get_review_analytics()
    assert analytics["summary"]["reviewed"] == 1  # only obs1


def test_stale_review_is_refused(temp_storage):
    obs1 = _obs(temp_storage, "g1", "dev1", "motion_sensor", "motion")
    case_id = temp_storage.upsert_review_case("dev1", "motion_sensor", "motion", obs1)

    obs2 = _obs(temp_storage, "g2", "dev1", "motion_sensor", "motion")
    # last_observation_id moves on to obs2 before the "reviewer" acts on obs1
    temp_storage.upsert_review_case("dev1", "motion_sensor", "motion", obs2)

    result = temp_storage.resolve_review_case(
        case_id, expected_observation_id=obs1, decision="approved"
    )
    assert result == "stale"
    assert temp_storage.count_pending_reviews() == 1  # untouched, still pending


# ---------------------------------------------------------------------------
# STEP 6.3: human decisions are immutable (pending -> resolved only)
# ---------------------------------------------------------------------------

def _decision_state(temp_storage, case_id, obs_id):
    """Snapshot of every field a resolution writes, on both tables."""
    conn = temp_storage.connect()
    case = conn.execute(
        "SELECT status, decision, corrected_category, decided_at "
        "FROM review_cases WHERE id = ?", (case_id,)
    ).fetchone()
    obs = conn.execute(
        "SELECT review_status, human_decision, corrected_category, reviewed_at "
        "FROM classification_observations WHERE id = ?", (obs_id,)
    ).fetchone()
    return dict(case), dict(obs)


def _resolved_case(temp_storage, decision, corrected_category=None):
    obs = _obs(temp_storage, "g1", "dev1", "env", "environmental")
    case_id = temp_storage.upsert_review_case("dev1", "env", "environmental", obs)
    result = temp_storage.resolve_review_case(
        case_id, expected_observation_id=obs, decision=decision,
        corrected_category=corrected_category,
    )
    assert result == "resolved"
    return case_id, obs


def test_pending_to_approved_succeeds(temp_storage):
    case_id, obs = _resolved_case(temp_storage, "approved")
    case, row = _decision_state(temp_storage, case_id, obs)
    assert case["status"] == "resolved"
    assert case["decision"] == "approved"
    assert row["human_decision"] == "approved"


def test_pending_to_corrected_succeeds(temp_storage):
    case_id, obs = _resolved_case(temp_storage, "corrected", "occupancy")
    case, row = _decision_state(temp_storage, case_id, obs)
    assert case["decision"] == "corrected"
    assert case["corrected_category"] == "occupancy"
    assert row["corrected_category"] == "occupancy"


def test_approved_cannot_be_re_resolved_as_corrected(temp_storage):
    case_id, obs = _resolved_case(temp_storage, "approved")
    result = temp_storage.resolve_review_case(
        case_id, expected_observation_id=obs,
        decision="corrected", corrected_category="occupancy",
    )
    assert result == "already_resolved"
    case, row = _decision_state(temp_storage, case_id, obs)
    assert case["decision"] == "approved" and case["corrected_category"] is None
    assert row["human_decision"] == "approved" and row["corrected_category"] is None


def test_corrected_cannot_be_re_resolved_as_approved(temp_storage):
    case_id, obs = _resolved_case(temp_storage, "corrected", "occupancy")
    result = temp_storage.resolve_review_case(
        case_id, expected_observation_id=obs, decision="approved"
    )
    assert result == "already_resolved"
    case, row = _decision_state(temp_storage, case_id, obs)
    assert case["decision"] == "corrected"
    assert row["human_decision"] == "corrected"


def test_refused_re_resolution_leaves_original_decision_untouched(temp_storage):
    case_id, obs = _resolved_case(temp_storage, "corrected", "occupancy")
    before = _decision_state(temp_storage, case_id, obs)

    for decision, category in (("approved", None), ("rejected", None),
                               ("corrected", "environmental")):
        result = temp_storage.resolve_review_case(
            case_id, expected_observation_id=obs,
            decision=decision, corrected_category=category,
        )
        assert result == "already_resolved"

    # decision, corrected_category and decided_at/reviewed_at all unchanged
    assert _decision_state(temp_storage, case_id, obs) == before


def test_resolved_case_with_newer_evidence_reports_already_resolved(temp_storage):
    # After resolution, a later observation advances last_observation_id.
    # A resolve attempt is refused as already_resolved (the fundamental
    # reason), not as stale.
    case_id, obs1 = _resolved_case(temp_storage, "approved")
    obs2 = _obs(temp_storage, "g2", "dev1", "env", "environmental")
    temp_storage.upsert_review_case("dev1", "env", "environmental", obs2)

    result = temp_storage.resolve_review_case(
        case_id, expected_observation_id=obs2, decision="rejected"
    )
    assert result == "already_resolved"
    case, _ = _decision_state(temp_storage, case_id, obs1)
    assert case["decision"] == "approved"


def test_resolve_refuses_to_overwrite_already_labelled_observation(temp_storage):
    # Defense in depth: a pending case whose evidence row already carries
    # a human_decision (only reachable via the legacy *_observation()
    # methods) must not have that decision overwritten by the dual-write.
    obs = _obs(temp_storage, "g1", "dev1", "env", "environmental")
    case_id = temp_storage.upsert_review_case("dev1", "env", "environmental", obs)
    with pytest.warns(DeprecationWarning):
        assert temp_storage.approve_observation(obs) == "resolved"

    with pytest.raises(RuntimeError):
        temp_storage.resolve_review_case(
            case_id, expected_observation_id=obs,
            decision="corrected", corrected_category="occupancy",
        )
    case, row = _decision_state(temp_storage, case_id, obs)
    assert case["status"] == "pending"  # rolled back
    assert row["human_decision"] == "approved"


def test_older_observation_cannot_replace_newer_case_evidence(temp_storage):
    conn = temp_storage.connect()

    # Rows with controlled created_at are INSERTed directly: since M1 the
    # raw created_at of an existing observation cannot be rewritten.
    o1 = _obs_at(temp_storage, "o1", "dev1", "motion_sensor", "motion",
                 "2026-08-01T00:00:00Z")
    o2 = _obs_at(temp_storage, "o2", "dev1", "motion_sensor", "motion",
                 "2026-08-02T00:00:00Z")

    # Upsert the NEWER observation first, then the OLDER one arrives late
    # (out-of-order commit) - the case must not regress to o1.
    temp_storage.upsert_review_case("dev1", "motion_sensor", "motion", o2)
    temp_storage.upsert_review_case("dev1", "motion_sensor", "motion", o1)

    cursor = conn.cursor()
    cursor.execute(
        "SELECT last_observation_id FROM review_cases "
        "WHERE device_id='dev1' AND classifier_name='motion_sensor' "
        "AND hypothesis_category='motion'"
    )
    assert cursor.fetchone()['last_observation_id'] == o2


def _label(temp_storage, obs_id, decision, corrected_category=None):
    """Directly mark an observation as human-labelled, bypassing
    resolve_review_case(). Used only to set up test fixtures for
    get_correction_patterns() - this is NOT the production write path
    (that's resolve_review_case()'s dual-write), but the read side under
    test only cares about the resulting classification_observations
    columns, not how they got there."""
    conn = temp_storage.connect()
    conn.execute(
        "UPDATE classification_observations "
        "SET review_status='reviewed', human_decision=?, corrected_category=?, "
        "reviewed_at=? WHERE id=?",
        (decision, corrected_category, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), obs_id),
    )
    conn.commit()


def test_get_correction_patterns_empty_db(temp_storage):
    assert temp_storage.get_correction_patterns() == []


def test_get_correction_patterns_single_correction(temp_storage):
    obs = _obs(temp_storage, "g1", "dev1", "env", "environmental")
    _label(temp_storage, obs, "corrected", corrected_category="temperature_sensor")

    patterns = temp_storage.get_correction_patterns()
    assert len(patterns) == 1
    assert patterns[0]["classifier_name"] == "env"
    assert patterns[0]["hypothesis_category"] == "environmental"
    assert patterns[0]["corrected_category"] == "temperature_sensor"
    assert patterns[0]["correction_count"] == 1
    assert patterns[0]["sample_size"] == 1


def test_get_correction_patterns_aggregates_identical_triple(temp_storage):
    obs1 = _obs(temp_storage, "g1", "dev1", "env", "environmental")
    _label(temp_storage, obs1, "corrected", corrected_category="temperature_sensor")
    obs2 = _obs(temp_storage, "g2", "dev2", "env", "environmental")
    _label(temp_storage, obs2, "corrected", corrected_category="temperature_sensor")

    patterns = temp_storage.get_correction_patterns()
    assert len(patterns) == 1
    assert patterns[0]["correction_count"] == 2
    assert patterns[0]["sample_size"] == 2


def test_get_correction_patterns_different_target_category_separate_row(temp_storage):
    obs1 = _obs(temp_storage, "g1", "dev1", "env", "environmental")
    _label(temp_storage, obs1, "corrected", corrected_category="temperature_sensor")
    obs2 = _obs(temp_storage, "g2", "dev2", "env", "environmental")
    _label(temp_storage, obs2, "corrected", corrected_category="humidity_sensor")

    patterns = temp_storage.get_correction_patterns()
    corrected_to = {p["corrected_category"] for p in patterns}
    assert corrected_to == {"temperature_sensor", "humidity_sensor"}
    assert len(patterns) == 2
    for p in patterns:
        assert p["correction_count"] == 1
        assert p["sample_size"] == 2  # denominator is per (classifier, hypothesis), shared


def test_get_correction_patterns_excludes_approved_and_rejected(temp_storage):
    obs1 = _obs(temp_storage, "g1", "dev1", "env", "environmental")
    _label(temp_storage, obs1, "approved")
    obs2 = _obs(temp_storage, "g2", "dev2", "env", "environmental")
    _label(temp_storage, obs2, "rejected")

    patterns = temp_storage.get_correction_patterns()
    assert patterns == []  # no corrections, but sample_size logic is exercised elsewhere


def test_get_correction_patterns_ignores_review_cases_last_observation_id(temp_storage):
    """Regression guard for the exact bug the STEP 6 audit caught in
    production ('T & H Sensor'): review_cases.last_observation_id can
    point at a DIFFERENT, unlabelled observation than the one carrying
    the actual human_decision, once a later identical hypothesis arrives
    after resolution. get_correction_patterns() must find the correction
    via classification_observations directly, never via review_cases."""
    obs1 = _obs(temp_storage, "g1", "dev1", "env", "environmental")
    case_id = temp_storage.upsert_review_case("dev1", "env", "environmental", obs1)
    # Labels obs1 via the production dual-write (STEP 6.3: an observation
    # can be labelled only once, so no separate _label() fixture here).
    assert temp_storage.resolve_review_case(
        case_id, expected_observation_id=obs1, decision="corrected",
        corrected_category="temperature_sensor",
    ) == "resolved"

    # A later, unlabelled observation for the same logical key arrives -
    # review_cases.last_observation_id now points away from obs1.
    obs2 = _obs(temp_storage, "g2", "dev1", "env", "environmental")
    temp_storage.upsert_review_case("dev1", "env", "environmental", obs2)

    conn = temp_storage.connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT last_observation_id FROM review_cases WHERE id=?", (case_id,)
    )
    assert cursor.fetchone()["last_observation_id"] == obs2  # precondition of the test

    patterns = temp_storage.get_correction_patterns()
    assert len(patterns) == 1
    assert patterns[0]["correction_count"] == 1
    assert patterns[0]["corrected_category"] == "temperature_sensor"


def test_get_correction_patterns_multiple_classifiers_stay_separate(temp_storage):
    obs1 = _obs(temp_storage, "g1", "dev1", "motion_sensor", "motion")
    _label(temp_storage, obs1, "corrected", corrected_category="occupancy")
    obs2 = _obs(temp_storage, "g2", "dev2", "environmental_sensor", "motion")
    _label(temp_storage, obs2, "corrected", corrected_category="occupancy")

    patterns = temp_storage.get_correction_patterns()
    assert len(patterns) == 2
    classifiers = {p["classifier_name"] for p in patterns}
    assert classifiers == {"motion_sensor", "environmental_sensor"}
    for p in patterns:
        assert p["sample_size"] == 1  # each classifier has its own denominator


def test_get_correction_patterns_is_read_only(temp_storage):
    obs = _obs(temp_storage, "g1", "dev1", "env", "environmental")
    _label(temp_storage, obs, "corrected", corrected_category="temperature_sensor")

    conn = temp_storage.connect()
    before_obs = conn.execute(
        "SELECT COUNT(*) FROM classification_observations"
    ).fetchone()[0]
    before_cases = conn.execute("SELECT COUNT(*) FROM review_cases").fetchone()[0]

    temp_storage.get_correction_patterns()

    after_obs = conn.execute(
        "SELECT COUNT(*) FROM classification_observations"
    ).fetchone()[0]
    after_cases = conn.execute("SELECT COUNT(*) FROM review_cases").fetchone()[0]
    assert before_obs == after_obs
    assert before_cases == after_cases


def test_get_correction_patterns_deterministic_ordering(temp_storage):
    obs_a = _obs(temp_storage, "g1", "dev1", "env", "environmental")
    _label(temp_storage, obs_a, "corrected", corrected_category="a_category")
    obs_b = _obs(temp_storage, "g2", "dev2", "env", "environmental")
    _label(temp_storage, obs_b, "corrected", corrected_category="a_category")
    obs_c = _obs(temp_storage, "g3", "dev3", "env", "environmental")
    _label(temp_storage, obs_c, "corrected", corrected_category="b_category")

    # Run twice - ordering must be stable, not incidental to SQLite's
    # unspecified default tie-breaking.
    first = temp_storage.get_correction_patterns()
    second = temp_storage.get_correction_patterns()
    assert first == second
    assert first[0]["correction_count"] == 2  # a_category has 2, sorted first
    assert first[0]["corrected_category"] == "a_category"


def test_get_correction_patterns_raises_on_integrity_violation(temp_storage):
    """Two labelled rows for the same logical key (device_id,
    classifier_name, hypothesis_category) must hard-fail, not be
    silently summed, skipped, or resolved by picking the newest."""
    obs1 = _obs(temp_storage, "g1", "dev1", "env", "environmental")
    _label(temp_storage, obs1, "corrected", corrected_category="temperature_sensor")
    obs2 = _obs(temp_storage, "g2", "dev1", "env", "environmental")  # SAME device_id/classifier/category
    _label(temp_storage, obs2, "approved")

    with pytest.raises(RuntimeError, match="integrity assumption violated"):
        temp_storage.get_correction_patterns()


def test_backfill_reconstructs_state_without_reopening(temp_storage):
    a1 = _obs(temp_storage, "g1", "devX", "env", "categoryA", name="DX")
    conn = temp_storage.connect()
    conn.execute(
        "UPDATE classification_observations SET review_status='reviewed', "
        "human_decision='approved', reviewed_at=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), a1),
    )
    conn.commit()

    # a2: a later, noisy observation reproducing the same hypothesis -
    # arrives AFTER a1 was already reviewed, and is never itself reviewed.
    _obs(temp_storage, "g2", "devX", "env", "categoryA", name="DX")

    result = temp_storage.backfill_review_cases()
    assert result["created"] == 1

    assert temp_storage.count_pending_reviews() == 0  # resolved, not reopened by a2


# ---------------------------------------------------------------------------
# STEP 6.4: legacy observation-level decision methods are immutable too
# ---------------------------------------------------------------------------

_DECISION_FIELDS = (
    "review_status, human_decision, corrected_category, "
    "reviewed_at, review_reason, reviewed_by"
)


def _obs_decision(temp_storage, obs_id):
    row = temp_storage.connect().execute(
        f"SELECT {_DECISION_FIELDS} FROM classification_observations WHERE id = ?",
        (obs_id,),
    ).fetchone()
    return dict(row)


def _legacy_decide(temp_storage, obs_id, decision):
    """Call the legacy method for decision ('approved'|'rejected'|'corrected')."""
    with pytest.warns(DeprecationWarning):
        if decision == "approved":
            return temp_storage.approve_observation(obs_id, reason="first")
        if decision == "rejected":
            return temp_storage.reject_observation(obs_id, reason="first")
        return temp_storage.correct_observation(obs_id, "occupancy", reason="first")


@pytest.mark.parametrize("decision", ["approved", "rejected", "corrected"])
def test_legacy_first_decision_succeeds(temp_storage, decision):
    obs = _obs(temp_storage, "g1", "dev1", "env", "environmental")
    result = _legacy_decide(temp_storage, obs, decision)
    assert result == "resolved"
    assert result  # truthy on success, as the old bool contract was
    state = _obs_decision(temp_storage, obs)
    assert state["human_decision"] == decision
    assert state["review_status"] == "reviewed"
    assert state["corrected_category"] == ("occupancy" if decision == "corrected" else None)


@pytest.mark.parametrize("first, second", [
    ("approved", "corrected"),
    ("corrected", "approved"),
    ("rejected", "approved"),
])
def test_legacy_second_decision_is_refused_and_changes_nothing(temp_storage, first, second):
    obs = _obs(temp_storage, "g1", "dev1", "env", "environmental")
    _legacy_decide(temp_storage, obs, first)
    before = _obs_decision(temp_storage, obs)

    result = _legacy_decide(temp_storage, obs, second)

    assert result == "already_resolved"
    assert not result  # an old `if storage.approve_observation(...)` sees failure
    # every decision field and timestamp unchanged
    assert _obs_decision(temp_storage, obs) == before


def test_legacy_cannot_overwrite_decision_made_by_resolve_review_case(temp_storage):
    case_id, obs = _resolved_case(temp_storage, "approved")
    before = _obs_decision(temp_storage, obs)

    result = _legacy_decide(temp_storage, obs, "corrected")

    assert result == "already_resolved"
    assert _obs_decision(temp_storage, obs) == before


@pytest.mark.parametrize("decision", ["approved", "rejected", "corrected"])
def test_legacy_missing_observation_is_not_found_and_falsy(temp_storage, decision):
    result = _legacy_decide(temp_storage, "obs_does_not_exist", decision)
    assert result == "not_found"
    assert not result  # unchanged from the old `return False`


def test_decision_result_truth_values():
    from src.storage import DecisionResult
    assert bool(DecisionResult("resolved")) is True
    assert bool(DecisionResult("already_resolved")) is False
    assert bool(DecisionResult("not_found")) is False
    # Documented compatibility boundary: direct bool comparison is NOT supported.
    assert (DecisionResult("resolved") == True) is False  # noqa: E712
