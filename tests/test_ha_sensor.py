#!/usr/bin/env python3
"""
STEP 4A.2 Test: ha_sensor module
- publish_pending_reviews succeeds when SUPERVISOR_TOKEN set and POST succeeds
- publish_pending_reviews fails gracefully (returns False) when token missing
- publish_pending_reviews fails gracefully (returns False) on network error
- update_pending_reviews counts via a real Storage + publishes via mocked HTTP
- Nothing here makes a real network call

STEP 5 update: count_pending_reviews() now counts review_cases.status=
'pending', not raw classification_observations rows. insert_test_observation()
now also drives the real review_cases path (upsert_review_case() +, for
review_status="reviewed", resolve_review_case()) so this test exercises
production behavior rather than a stale table that count_pending_reviews()
no longer reads.
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

    # STEP 5: drive the real review_cases path so count_pending_reviews()
    # (now counting review_cases, not raw observation rows) reflects this
    # observation correctly. Each obs_id gets its own device_id, so
    # obs_1/obs_2 remain two independent pending cases; obs_3 is walked
    # through the real resolve path so it stops counting as pending -
    # matching the original test's intent of "3 inserted, 2 pending".
    case_id = storage.upsert_review_case(
        device_id=f"device_{obs_id}",
        classifier_name="motion_sensor",
        hypothesis_category="motion_sensor",
        observation_id=obs_id,
    )
    if review_status == "reviewed":
        storage.resolve_review_case(
            case_id, expected_observation_id=obs_id, decision="approved"
        )


def test_publish_success():
    print("1. Testing publish_pending_reviews() success path...")
    with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "fake-token"}):
        with patch("src.ha_sensor.requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            result = ha_sensor.publish_pending_reviews(3)

            if result is not True:
                print(f"   âś— Expected True, got {result}")
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
                    print(f"   âś— {msg}")
                    return False
            print(f"   âś“ POST called correctly, no real network hit")
    print()
    return True


def test_publish_no_token():
    print("2. Testing publish_pending_reviews() with no SUPERVISOR_TOKEN...")
    env_without_token = {k: v for k, v in os.environ.items() if k != "SUPERVISOR_TOKEN"}
    with patch.dict(os.environ, env_without_token, clear=True):
        with patch("src.ha_sensor.requests.post") as mock_post:
            result = ha_sensor.publish_pending_reviews(5)

            if result is not False:
                print(f"   âś— Expected False, got {result}")
                return False
            if mock_post.called:
                print(f"   âś— requests.post should NOT have been called")
                return False
            print("   âś“ Returns False, no HTTP call attempted")
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
                print(f"   âś— Exception propagated (should have been caught): {exc}")
                return False

            if result is not False:
                print(f"   âś— Expected False, got {result}")
                return False
            print("   âś“ Returns False, exception caught and not propagated")
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
                    print(f"   âś— Expected count 2, got {result}")
                    return False

                payload = mock_post.call_args[1]["json"]
                if payload["state"] != "2":
                    print(f"   âś— Published state should be '2', got {payload['state']}")
                    return False

                print(f"   âś“ Counted 2 pending, published state='2' via mocked HTTP")

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
        print("âś“ ALL TESTS PASSED")
        print("=" * 60)
        return True
    else:
        print("=" * 60)
        print(f"âś— {results.count(False)} TEST(S) FAILED")
        print("=" * 60)
        return False


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
