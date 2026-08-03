#!/usr/bin/env python3
"""
STEP 4A.2 Test: thread-safety of count_pending_reviews()

Reproduces the exact failure scenario described in review:
1. Main thread creates Storage and self.connection (via a call that
   uses self.connect(), e.g. approve_observation()).
2. count_pending_reviews() is then called from a DIFFERENT thread
   (simulating asyncio.to_thread()'s executor thread pool).

Before the fix: this would raise
    sqlite3.ProgrammingError: SQLite objects created in a thread can
    only be used in that same thread
because count_pending_reviews() used to call self.connect(), reusing
the connection object created on the main thread.

After the fix: count_pending_reviews() opens and closes its own
connection, so it works correctly regardless of which thread calls it.
"""

import asyncio
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.storage import Storage


def insert_test_observation(storage, obs_id):
    conn = storage.connect()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO classification_observations "
        "(id, observation_group_id, device_id, classifier_name, "
        "hypothesis_category, hypothesis_confidence, device_source_adapter, "
        "created_at, device_name) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (obs_id, f"group_{obs_id}", f"device_{obs_id}", "motion_sensor",
         "motion_sensor", 0.8, "ha", "2026-08-03T00:00:00Z", f"Test {obs_id}")
    )
    conn.commit()


def test_cross_thread_count():
    print("1. Testing count_pending_reviews() called from a different")
    print("   thread than the one that created self.connection...")
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        storage = Storage(db_path=db_path)

        # Step 1: force self.connection to be created on the MAIN thread,
        # exactly like approve_observation()/save_asset()/etc. would.
        insert_test_observation(storage, "obs_main_thread")
        storage.connect()  # explicitly ensure self.connection exists now
        main_thread_conn = storage.connection
        print(f"   self.connection created on main thread: {threading.current_thread().name}")

        # Step 2: call count_pending_reviews() from a DIFFERENT thread,
        # simulating asyncio.to_thread()'s executor pool.
        result_holder = {}
        error_holder = {}

        def worker():
            try:
                result_holder["count"] = storage.count_pending_reviews()
            except Exception as exc:
                error_holder["error"] = exc

        worker_thread = threading.Thread(target=worker, name="worker-thread-simulating-to_thread")
        worker_thread.start()
        worker_thread.join(timeout=5)

        if "error" in error_holder:
            print(f"   ✗ FAILED: cross-thread call raised: {error_holder['error']}")
            print(f"     (this is the exact bug the fix addresses)")
            return False

        if "count" not in result_holder:
            print("   ✗ FAILED: worker thread did not complete")
            return False

        if result_holder["count"] == 1:
            print(f"   ✓ PASS: count_pending_reviews() returned {result_holder['count']} "
                  f"from a different thread, no ProgrammingError raised")
        else:
            print(f"   ✗ FAILED: expected count=1, got {result_holder['count']}")
            return False

        # Step 3: confirm the main thread's connection is untouched and
        # still usable afterward (proves no shared-state corruption).
        print()
        print("2. Testing main thread's connection still works after...")
        insert_test_observation(storage, "obs_after")
        conn = storage.connect()
        if conn is main_thread_conn:
            print("   ✓ Main thread connection object unchanged, still usable")
        else:
            print("   ✗ Main thread connection was unexpectedly replaced")
            return False

    print()
    return True


async def test_via_actual_asyncio_to_thread():
    print("3. Testing via real asyncio.to_thread() (not simulated)...")
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        storage = Storage(db_path=db_path)

        # Create self.connection on the event loop's thread first
        insert_test_observation(storage, "obs_async_main")
        storage.connect()

        # Now call count_pending_reviews() through the real
        # asyncio.to_thread(), exactly as core.py/main.py do
        try:
            count = await asyncio.to_thread(storage.count_pending_reviews)
        except Exception as exc:
            print(f"   ✗ FAILED: asyncio.to_thread() call raised: {exc}")
            return False

        if count == 1:
            print(f"   ✓ PASS: real asyncio.to_thread() call succeeded, count={count}")
        else:
            print(f"   ✗ FAILED: expected count=1, got {count}")
            return False

    print()
    return True


def main():
    print("=" * 60)
    print("STEP 4A.2: Thread-safety verification")
    print("=" * 60)
    print()

    result1 = test_cross_thread_count()
    result2 = asyncio.run(test_via_actual_asyncio_to_thread())

    if result1 and result2:
        print("=" * 60)
        print("✓ ALL TESTS PASSED")
        print("=" * 60)
        return True
    else:
        print("=" * 60)
        print("✗ SOME TESTS FAILED")
        print("=" * 60)
        return False


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
