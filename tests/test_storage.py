# tests/test_storage.py

import pytest
from pathlib import Path
import tempfile
from datetime import datetime

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
        'lifecycle_discovered_at': datetime.utcnow().isoformat() + 'Z'
    }

    asset_id = temp_storage.save_asset(asset)
    assert asset_id == 'test_asset_1'

    loaded = temp_storage.load_asset(asset_id)
    assert loaded is not None
    assert loaded['name'] == 'Test Device'

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
        'lifecycle_discovered_at': datetime.utcnow().isoformat() + 'Z'
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


def test_backfill_reconstructs_state_without_reopening(temp_storage):
    a1 = _obs(temp_storage, "g1", "devX", "env", "categoryA", name="DX")
    conn = temp_storage.connect()
    conn.execute(
        "UPDATE classification_observations SET review_status='reviewed', "
        "human_decision='approved', reviewed_at=? WHERE id=?",
        (datetime.utcnow().isoformat() + 'Z', a1),
    )
    conn.commit()

    # a2: a later, noisy observation reproducing the same hypothesis -
    # arrives AFTER a1 was already reviewed, and is never itself reviewed.
    _obs(temp_storage, "g2", "devX", "env", "categoryA", name="DX")

    result = temp_storage.backfill_review_cases()
    assert result["created"] == 1

    assert temp_storage.count_pending_reviews() == 0  # resolved, not reopened by a2
