#!/usr/bin/env python3
"""
STEP 3C (v2) Test: verify all four review fixes
- reviewed_by column present and stored correctly
- rowcount checked before commit (unknown ID leaves no partial state)
- migration still idempotent with 6 columns total
- original approve/reject/correct behavior unchanged
"""

import sqlite3
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from storage_step3c_final import Storage


def insert_test_observation(storage, obs_id, category="motion_sensor", confidence=0.6):
    conn = storage.connect()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO classification_observations "
        "(id, observation_group_id, device_id, classifier_name, "
        "hypothesis_category, hypothesis_confidence, device_source_adapter, "
        "created_at, device_name) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (obs_id, f"group_{obs_id}", f"device_{obs_id}", "motion_sensor",
         category, confidence, "ha", "2026-08-03T00:00:00Z",
         f"Test Device {obs_id}")
    )
    conn.commit()


def test_step3c_v2():
    print("=" * 60)
    print("STEP 3C (v2): Fixes verification")
    print("=" * 60)
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        storage = Storage(db_path=db_path)
        print()

        # Test 1: all 6 review columns present (5 from before + reviewed_by)
        print("1. Verifying all 6 review columns exist...")
        conn = storage.connect()
        cursor = conn.cursor()
        columns = {row[1] for row in cursor.execute(
            "PRAGMA table_info(classification_observations)"
        ).fetchall()}
        expected = {
            "review_status", "human_decision", "reviewed_at",
            "review_reason", "corrected_category", "reviewed_by"
        }
        missing = expected - columns
        if not missing:
            print(f"   ✓ All 6 columns present: {sorted(expected)}")
        else:
            print(f"   ✗ Missing: {missing}")
            return False
        print()

        insert_test_observation(storage, "obs_a", "motion_sensor", 0.9)
        insert_test_observation(storage, "obs_b", "energy_meter", 0.55)
        print("✓ Inserted 2 test observations")
        print()

        # Test 2: reviewed_by stored correctly (default)
        print("2. Testing reviewed_by default value...")
        storage.approve_observation("obs_a", reason="looks fine")
        cursor.execute(
            "SELECT reviewed_by FROM classification_observations WHERE id = ?",
            ("obs_a",)
        )
        row = cursor.fetchone()
        if row[0] == "human":
            print(f"   ✓ reviewed_by defaulted to 'human'")
        else:
            print(f"   ✗ Unexpected reviewed_by: {row[0]}")
            return False
        print()

        # Test 3: reviewed_by explicit override
        print("3. Testing reviewed_by explicit value...")
        storage.reject_observation("obs_b", reason="wrong type", reviewed_by="Marek")
        cursor.execute(
            "SELECT reviewed_by FROM classification_observations WHERE id = ?",
            ("obs_b",)
        )
        row = cursor.fetchone()
        if row[0] == "Marek":
            print(f"   ✓ reviewed_by explicitly set to 'Marek'")
        else:
            print(f"   ✗ Unexpected reviewed_by: {row[0]}")
            return False
        print()

        # Test 4: rowcount-before-commit - unknown ID leaves no trace
        print("4. Testing unknown observation_id (rollback, no partial commit)...")
        insert_test_observation(storage, "obs_c", "motion_sensor", 0.7)
        before = dict(cursor.execute(
            "SELECT review_status, human_decision, reviewed_by "
            "FROM classification_observations WHERE id = 'obs_c'"
        ).fetchone())

        result = storage.approve_observation("obs_does_not_exist", reviewed_by="Marek")
        if result is not False:
            print(f"   ✗ Expected False, got {result}")
            return False

        # Verify obs_c (a real, unrelated row) was not touched
        after = dict(cursor.execute(
            "SELECT review_status, human_decision, reviewed_by "
            "FROM classification_observations WHERE id = 'obs_c'"
        ).fetchone())
        if before == after and after["review_status"] == "pending":
            print("   ✓ Returns False, unrelated row untouched, still pending")
        else:
            print(f"   ✗ State changed unexpectedly: before={before} after={after}")
            return False
        print()

        # Test 5: correct_observation still preserves original + stores reviewed_by
        print("5. Testing correct_observation() with reviewed_by...")
        storage.correct_observation(
            "obs_c", "environmental_sensor", reason="actually has sensors",
            reviewed_by="ACE"
        )
        cursor.execute(
            "SELECT hypothesis_category, corrected_category, reviewed_by "
            "FROM classification_observations WHERE id = 'obs_c'"
        )
        row = cursor.fetchone()
        if row[0] == "motion_sensor" and row[1] == "environmental_sensor" and row[2] == "ACE":
            print(f"   ✓ Original preserved, correction + reviewer stored: {tuple(row)}")
        else:
            print(f"   ✗ Unexpected: {tuple(row)}")
            return False
        print()

        # Test 6: get_pending_reviews returns new fields
        print("6. Testing get_pending_reviews() includes new fields...")
        insert_test_observation(storage, "obs_d", "motion_sensor", 0.8)
        pending = storage.get_pending_reviews()
        if len(pending) == 1 and "reviewed_by" in pending[0] and "corrected_category" in pending[0]:
            print(f"   ✓ Pending observation includes reviewed_by and corrected_category fields")
        else:
            print(f"   ✗ Fields missing or wrong count: {pending}")
            return False
        print()

        # Test 7: migration idempotency holds with 6 columns
        print("7. Testing migration idempotency (second init, 6 columns)...")
        storage2 = Storage(db_path=db_path)
        print("   ✓ Second init completed without error")
        print()

        print("=" * 60)
        print("✓ ALL TESTS PASSED")
        print("=" * 60)
        return True


if __name__ == "__main__":
    success = test_step3c_v2()
    sys.exit(0 if success else 1)
