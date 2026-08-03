# src/ha_sensor.py
"""
Home Assistant sensor publisher for Cognitive Core.

Publishes the pending-review count to Home Assistant via the
Supervisor REST API proxy, so the Learning Loop review queue is
visible as a native HA entity (sensor.cognitive_core_pending_reviews)
without any manual Home Assistant configuration.

STEP 4A.2: Event-driven sensor updates.

Design note - synchronous requests, not aiohttp:
This module is called from two different execution contexts:
  1. The async main process (CognitiveCore.start(), on_device_detected(),
     and the periodic reconcile loop in main.py's main_loop())
  2. The synchronous CLI (main.py's show_review_queue(), invoked via
     `docker exec ... python -m src.main review`)
A single synchronous implementation avoids maintaining two versions of
the same logic. The blocking HTTP call targets the local Supervisor
proxy (not a real network round trip), and this codebase already makes
blocking sqlite3 calls from inside async functions elsewhere (Storage),
so this follows the same existing pattern rather than introducing a
new one.

Design note - failures never raise:
A sensor push failure must never interrupt classification, review
decisions, or the main loop. Every failure path here logs and returns
a falsy value instead of raising.
"""

import os
from datetime import datetime, timezone
from typing import Optional

import requests

SENSOR_ENTITY_ID = "sensor.cognitive_core_pending_reviews"
SUPERVISOR_API_BASE = "http://supervisor/core/api"
REQUEST_TIMEOUT_SECONDS = 5


def _supervisor_token() -> Optional[str]:
    return os.getenv("SUPERVISOR_TOKEN")


def publish_pending_reviews(count: int) -> bool:
    """Publish the pending review count to Home Assistant as a sensor.

    Args:
        count: current number of pending observations

    Returns:
        True if the push succeeded, False otherwise (including when
        SUPERVISOR_TOKEN is not set - e.g. running outside the add-on).
        Never raises.
    """
    token = _supervisor_token()
    if not token:
        print("[HA_SENSOR] WARNING: SUPERVISOR_TOKEN not set, skipping sensor push")
        return False

    url = f"{SUPERVISOR_API_BASE}/states/{SENSOR_ENTITY_ID}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "state": str(count),
        "attributes": {
            "friendly_name": "Cognitive Core Pending Reviews",
            "icon": "mdi:clipboard-check",
            "unit_of_measurement": "reviews",
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "source": "classification_observations",
        },
    }

    try:
        response = requests.post(
            url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        print(f"[HA_SENSOR] Published {SENSOR_ENTITY_ID} = {count}")
        return True
    except requests.exceptions.RequestException as exc:
        print(f"[HA_SENSOR] WARNING: failed to publish sensor: {exc}")
        return False


def update_pending_reviews(storage) -> Optional[int]:
    """Count pending reviews and publish to Home Assistant in one call.

    This is the function callers should use - it owns both counting
    and publishing, so core.py and main.py don't need to know about
    Storage.count_pending_reviews() and publish_pending_reviews()
    separately.

    Args:
        storage: a Storage instance

    Returns:
        The count that was published, or None if counting itself
        failed. A publish failure does not affect the return value -
        see publish_pending_reviews() for that failure path. Never
        raises.
    """
    try:
        count = storage.count_pending_reviews()
    except Exception as exc:
        print(f"[HA_SENSOR] WARNING: failed to count pending reviews: {exc}")
        return None

    publish_pending_reviews(count)
    return count
