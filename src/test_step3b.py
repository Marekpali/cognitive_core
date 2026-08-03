#!/usr/bin/env python3
"""
STEP 3B Test: get_pending_reviews() data layer
- Verify new method reads classification_observations correctly
- Verify include_reviewed toggle works
- Verify renamed get_pending_asset_reviews() still works (no name collision)
- Verify limit parameter
"""

import sqlite3
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from storage_step3b import Storage


def insert_test_observation(storage, obs_id, review_status="pending", confidence=0.8):
    """Helper: insert a raw observation row for testing."""
    conn = storage.connect()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO classification_observations "
        "(id, observation_group_id, device_id, classifier_name, "
        "hypothesis_category, hypothesis_confidence, device_source_adapter, "
        "created_at, review_status, device_name) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (obs_id, f"group_{obs_id}", f"device_{obs_id}", "motion_sensor",
         "motion_sensor", confidence, "ha", "2026-08-03T00:00:00Z",
         review_status, f"Test Device {obs_id}")
    )
    conn.commit()


def test_get_pending_reviews():
    print("=" * 60)
    print("STEP 3B: get_pending_reviews() DATA LAYER TEST")
    print("=" * 60)
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        storage = Storage(db_path=db_path)
        print("✓ Storage initialized (STEP 3A migration ran)")
        print()

        # Insert mixed observations: 3 pending, 2 reviewed
        insert_test_observation(storage, "obs_1", "pending", 0.9)
        insert_test_observation(storage, "obs_2", "pending", 0.7)
        insert_test_observation(storage, "obs_3", "pending", 0.6)
        insert_test_observation(storage, "obs_4", "reviewed", 0.95)
        insert_test_observation(storage, "obs_5", "reviewed", 0.5)
        print("✓ Inserted 5 test observations (3 pending, 2 reviewed)")
        print()

        # Test 1: default call returns only pending
        print("1. Testing default call (include_reviewed=False)...")
        pending = storage.get_pending_reviews()
        if len(pending) == 3:
            print(f"   ✓ Returned {len(pending)} observations (expected 3)")
        else:
            print(f"   ✗ Returned {len(pending)} observations (expected 3)")
            return False

        ids = {r["id"] for r in pending}
        if ids == {"obs_1", "obs_2", "obs_3"}:
            print("   ✓ Correct observation IDs returned")
        else:
            print(f"   ✗ Wrong IDs: {ids}")
            return False
        print()

        # Test 2: include_reviewed=True returns all 5
        print("2. Testing include_reviewed=True...")
        all_obs = storage.get_pending_reviews(include_reviewed=True)
        if len(all_obs) == 5:
            print(f"   ✓ Returned {len(all_obs)} observations (expected 5)")
        else:
            print(f"   ✗ Returned {len(all_obs)} observations (expected 5)")
            return False
        print()

        # Test 3: limit parameter
        print("3. Testing limit parameter...")
        limited = storage.get_pending_reviews(limit=2)
        if len(limited) == 2:
            print(f"   ✓ Limit respected: {len(limited)} returned")
        else:
            print(f"   ✗ Limit not respected: {len(limited)} returned")
            return False
        print()

        # Test 4: field completeness
        print("4. Testing returned fields...")
        sample = pending[0]
        required_fields = {
            "id", "observation_group_id", "device_id", "device_name",
            "classifier_name", "hypothesis_category", "hypothesis_confidence",
            "hypothesis_reasoning", "review_status", "human_decision",
            "reviewed_at", "review_reason", "created_at"
        }
        missing = required_fields - set(sample.keys())
        if not missing:
            print(f"   ✓ All {len(required_fields)} expected fields present")
        else:
            print(f"   ✗ Missing fields: {missing}")
            return False
        print()

        # Test 5: ordering (most recent first - all same timestamp here,
        # so just verify no crash and correct count already covered above)
        print("5. Testing get_pending_asset_reviews() still works (no collision)...")
        try:
            legacy = storage.get_pending_asset_reviews()
            print(f"   ✓ get_pending_asset_reviews() callable, returned {len(legacy)} (empty table, expected 0)")
            if len(legacy) != 0:
                print(f"   ✗ Expected 0 (no review_queue/assets data seeded), got {len(legacy)}")
                return False
        except AttributeError as e:
            print(f"   ✗ Method missing or renamed incorrectly: {e}")
            return False
        print()

        print("=" * 60)
        print("✓ ALL TESTS PASSED")
        print("=" * 60)
        return True


if __name__ == "__main__":
    success = test_get_pending_reviews()
    sys.exit(0 if success else 1)
