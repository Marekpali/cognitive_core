#!/usr/bin/env python3
"""
STEP 4A.2 Test: verify sensor push doesn't block the event loop.

Simulates a slow HTTP call (2s) inside update_pending_reviews and
confirms that other async work continues running concurrently,
proving asyncio.to_thread() is actually offloading the blocking call
rather than it running inline on the event loop.
"""

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import ha_sensor


def slow_fake_post(*args, **kwargs):
    """Simulate a slow (2s) blocking HTTP call, like requests.post would be."""
    time.sleep(2)

    class FakeResponse:
        def raise_for_status(self):
            pass

    return FakeResponse()


class FakeStorage:
    def count_pending_reviews(self):
        return 1


async def other_async_work(marker_list):
    """Represents the HA adapter's listen() loop - must keep running
    even while a sensor push is in flight."""
    for i in range(20):
        marker_list.append(i)
        await asyncio.sleep(0.1)


async def main():
    print("=" * 60)
    print("STEP 4A.2: Event loop non-blocking verification")
    print("=" * 60)
    print()

    markers = []

    with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "fake-token"}):
        with patch("src.ha_sensor.requests.post", side_effect=slow_fake_post):
            storage = FakeStorage()

            start = time.monotonic()

            # Fire the "blocking" sensor push via to_thread, same pattern
            # used in core.py/main.py, alongside other async work that
            # must not be starved.
            background_tasks = set()
            task = asyncio.create_task(
                asyncio.to_thread(ha_sensor.update_pending_reviews, storage)
            )
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)

            other_task = asyncio.create_task(other_async_work(markers))

            await other_task
            published_count = await task

            elapsed = time.monotonic() - start

    print(f"Other async work completed {len(markers)} iterations "
          f"while a 2s 'HTTP call' was in flight via to_thread.")
    print(f"Total elapsed: {elapsed:.2f}s")
    print()

    # If to_thread worked, other_async_work (2.0s of sleep(0.1) x 20)
    # and the 2s blocking call overlap, so total elapsed should be
    # close to 2s, NOT ~4s (which would indicate serialization/blocking).
    if published_count is None:
        print("✗ FAIL: update_pending_reviews returned None - the slow HTTP "
              "path was never actually exercised (test would have passed "
              "vacuously). Check SUPERVISOR_TOKEN mocking.")
        return False

    if len(markers) == 20 and elapsed < 3.0:
        print(f"✓ PASS: event loop stayed responsive (elapsed {elapsed:.2f}s < 3.0s threshold)")
        print(f"  Slow path confirmed exercised: update_pending_reviews returned {published_count}")
        print("  If update_pending_reviews() had blocked the loop directly,")
        print("  this would have taken ~4s instead of ~2s.")
        return True
    else:
        print(f"✗ FAIL: elapsed={elapsed:.2f}s, markers={len(markers)}")
        print("  This suggests the blocking call was NOT properly offloaded.")
        return False


if __name__ == "__main__":
    result = asyncio.run(main())
    sys.exit(0 if result else 1)
