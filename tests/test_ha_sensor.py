#!/usr/bin/env python3
"""
STEP 4A.2 Test: ha_sensor module
- publish_pending_reviews succeeds when SUPERVISOR_TOKEN set and POST succeeds
- publish_pending_reviews fails gracefully (returns False) when token missing
- publish_pending_reviews fails gracefully (returns False) on network error
- update_pending_reviews counts via a real Storage + publishes via mocked HTTP
- Nothing here makes a real network call
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import ha_sensor
from src.storage import Storage


def insert_test_observation(storage, obs_id, review_status="pending"):
    conn = storage.connect()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO classification_observations "
        "(id, observation_group_id, device_id, classifier_name, "
        "hypothesis_category, hypothesis_confidence, device_source_adapter, "
        "created_at, review_status, device_name) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (obs_id, f"group_{obs_id}", f"device_{obs_id}", "motion_sensor",
         "motion_sensor", 0.8, "ha", "2026-08-03T00:00:00Z",
         review_status, f"Test Device {obs_id}")
    )
    conn.commit()


def test_publish_success():
    print("1. Testing publish_pending_reviews() success path...")
    with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "fake-token"}):
        with patch("src.ha_sensor.requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            result = ha_sensor.publish_pending_reviews(3)

            if result is not True:
                print(f"   ✗ Expected True, got {result}")
                return False

            call_args = mock_post.call_args
            url = call_args[0][0]
            payload = call_args[1]["json"]
            headers = call_args[1]["headers"]

            checks = [
                (url == "http://supervisor/core/api/states/sensor.cognitive_core_pending_reviews",
                 f"URL correct: {url}"),
                (payload["state"] == "3", f"state correct: {payload['state']}"),
                (payload["attributes"]["friendly_name"] == "Cognitive Core Pending Reviews",
                 "friendly_name correct"),
                (headers["Authorization"] == "Bearer fake-token",
                 "Authorization header correct"),
            ]
            for ok, msg in checks:
                if not ok:
                    print(f"   ✗ {msg}")
                    return False
            print(f"   ✓ POST called correctly, no real network hit")
    print()
    return True


def test_publish_no_token():
    print("2. Testing publish_pending_reviews() with no SUPERVISOR_TOKEN...")
    env_without_token = {k: v for k, v in os.environ.items() if k != "SUPERVISOR_TOKEN"}
    with patch.dict(os.environ, env_without_token, clear=True):
        with patch("src.ha_sensor.requests.post") as mock_post:
            result = ha_sensor.publish_pending_reviews(5)

            if result is not False:
                print(f"   ✗ Expected False, got {result}")
                return False
            if mock_post.called:
                print(f"   ✗ requests.post should NOT have been called")
                return False
            print("   ✓ Returns False, no HTTP call attempted")
    print()
    return True


def test_publish_network_error():
    print("3. Testing publish_pending_reviews() on network error...")
    with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "fake-token"}):
        with patch("src.ha_sensor.requests.post") as mock_post:
            mock_post.side_effect = ha_sensor.requests.exceptions.ConnectionError("boom")

            try:
                result = ha_sensor.publish_pending_reviews(2)
            except Exception as exc:
                print(f"   ✗ Exception propagated (should have been caught): {exc}")
                return False

            if result is not False:
                print(f"   ✗ Expected False, got {result}")
                return False
            print("   ✓ Returns False, exception caught and not propagated")
    print()
    return True


def test_update_pending_reviews_integration():
    print("4. Testing update_pending_reviews() - real Storage + mocked HTTP...")
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        storage = Storage(db_path=db_path)

        insert_test_observation(storage, "obs_1", "pending")
        insert_test_observation(storage, "obs_2", "pending")
        insert_test_observation(storage, "obs_3", "reviewed")

        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "fake-token"}):
            with patch("src.ha_sensor.requests.post") as mock_post:
                mock_response = MagicMock()
                mock_response.raise_for_status.return_value = None
                mock_post.return_value = mock_response

                result = ha_sensor.update_pending_reviews(storage)

                if result != 2:
                    print(f"   ✗ Expected count 2, got {result}")
                    return False

                payload = mock_post.call_args[1]["json"]
                if payload["state"] != "2":
                    print(f"   ✗ Published state should be '2', got {payload['state']}")
                    return False

                print(f"   ✓ Counted 2 pending, published state='2' via mocked HTTP")

        # Windows-specific: TemporaryDirectory's cleanup on __exit__ fails
        # with PermissionError if the cached storage.connection (opened
        # by insert_test_observation's storage.connect() calls above) is
        # still open - Windows refuses to remove a file held open by a
        # live handle, unlike Unix. Close explicitly before the `with`
        # block ends.
        if storage.connection:
            storage.connection.close()
    print()
    return True


def main():
    print("=" * 60)
    print("STEP 4A.2: ha_sensor module TEST")
    print("=" * 60)
    print()

    results = [
        test_publish_success(),
        test_publish_no_token(),
        test_publish_network_error(),
        test_update_pending_reviews_integration(),
    ]

    if all(results):
        print("=" * 60)
        print("✓ ALL TESTS PASSED")
        print("=" * 60)
        return True
    else:
        print("=" * 60)
        print(f"✗ {results.count(False)} TEST(S) FAILED")
        print("=" * 60)
        return False


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
