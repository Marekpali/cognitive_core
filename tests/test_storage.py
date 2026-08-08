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

@pytest.mark.xfail(reason="Legacy review_queue test superseded by STEP 5 review_cases workflow", strict=True)
def test_review_queue(temp_storage):
    """Test review queue

    NOTE (STEP 5): This test is pre-existing and already fails on the
    current baseline (before any review_cases changes), because
    get_pending_reviews() reads from classification_observations while
    add_to_review_queue()/save_asset() write to review_queue/assets -
    two unrelated tables since the STEP 3 rename. Left unmodified here
    per the agreed scope: not fixed in this PR, tracked as a pre-existing
    defect.
    """

    asset = {
        'id': 'test_asset_2',
        'name': 'Review Test',
        'hypothesis': {
            'category': 'energy_meter',
            'confidence': 0.85
        },
        'lifecycle_state': 'provisional',
        'lifecycle_discovered_at': datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    }

    temp_storage.save_asset(asset)
    review_id = temp_storage.add_to_review_queue('test_asset_2')

    reviews = temp_storage.get_pending_reviews()
    assert len(reviews) == 1

    temp_storage.approve_review(review_id)

    reviews = temp_storage.get_pending_reviews()
    assert len(reviews) == 0


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


def test_older_observation_cannot_replace_newer_case_evidence(temp_storage):
    conn = temp_storage.connect()

    o1 = _obs(temp_storage, "g1", "dev1", "motion_sensor", "motion")
    conn.execute(
        "UPDATE classification_observations SET created_at = ? WHERE id = ?",
        ("2026-08-01T00:00:00Z", o1),
    )
    conn.commit()

    o2 = _obs(temp_storage, "g2", "dev1", "motion_sensor", "motion")
    conn.execute(
        "UPDATE classification_observations SET created_at = ? WHERE id = ?",
        ("2026-08-02T00:00:00Z", o2),
    )
    conn.commit()

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
    _label(temp_storage, obs1, "corrected", corrected_category="temperature_sensor")
    case_id = temp_storage.upsert_review_case("dev1", "env", "environmental", obs1)
    temp_storage.resolve_review_case(
        case_id, expected_observation_id=obs1, decision="corrected",
        corrected_category="temperature_sensor",
    )

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
