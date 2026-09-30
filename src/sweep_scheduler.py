"""STEP 7P: decides when a sweep is due (docs/STEP7_OBSERVATION_SOURCES.md 7.3).

Pure and clock-agnostic: every method takes `now` (monotonic seconds).

- connect: first connection -> "startup", later ones -> "reconnect"; due now.
- registry event: due `debounce` seconds after the FIRST pending event
  (not sliding), so a burst collapses into one sweep and a continuous
  stream cannot postpone it forever.
- daily: `daily` seconds after the last completed sweep of any source.
- failed sweep: its sources are retried after `retry` seconds.
- sweep with missing-metadata skips: retried after `retry` seconds, at most
  MAX_METADATA_RETRIES times in a row.

take() hands out the due sources as a ticket and clears them, so events
arriving while that sweep runs schedule a new sweep instead of being lost.
"""

from typing import Optional

EVENT_DEBOUNCE_SECONDS = 30.0
DAILY_SWEEP_SECONDS = 24 * 60 * 60.0
RETRY_AFTER_FAILURE_SECONDS = 300.0
IDLE_WAIT_SECONDS = 60.0
# aiohttp treats receive(timeout=0) as "use the default" (wait forever), so
# never hand out a zero wait.
MIN_WAIT_SECONDS = 0.05
# A sweep that skipped devices for missing metadata (e.g. HA still starting,
# states not loaded yet) is retried, but only a bounded number of times so a
# permanently state-less entity cannot cause a sweep every few minutes.
MAX_METADATA_RETRIES = 3
METADATA_RETRY_SOURCE = "retry_missing_metadata"


class SweepScheduler:
    def __init__(self, debounce: float = EVENT_DEBOUNCE_SECONDS,
                 daily: float = DAILY_SWEEP_SECONDS,
                 retry: float = RETRY_AFTER_FAILURE_SECONDS):
        self.debounce = debounce
        self.daily = daily
        self.retry = retry
        self._pending: dict = {}          # source -> due time
        self._next_daily: Optional[float] = None
        self._connected_before = False
        self._metadata_retries = 0

    def on_connected(self, now: float) -> None:
        source = "reconnect" if self._connected_before else "startup"
        self._connected_before = True
        self._pending[source] = now

    def on_event(self, event_type: str, now: float) -> None:
        # Not sliding: a due time is only ever moved earlier, never later.
        # Joins an already-scheduled sweep if that runs sooner.
        due = min([now + self.debounce, *self._pending.values()])
        self._pending[event_type] = min(self._pending.get(event_type, due), due)

    def take(self, now: float) -> Optional[frozenset]:
        """Sources of the sweep to run now, or None if nothing is due."""
        if self._pending and min(self._pending.values()) <= now:
            ticket = frozenset(self._pending)
            self._pending = {}
            return ticket
        if self._next_daily is not None and now >= self._next_daily:
            self._next_daily = None
            return frozenset({"daily"})
        return None

    def mark_done(self, now: float, missing_metadata: int = 0) -> None:
        self._next_daily = now + self.daily
        if not missing_metadata:
            self._metadata_retries = 0
        elif self._metadata_retries < MAX_METADATA_RETRIES:
            self._metadata_retries += 1
            self._pending.setdefault(METADATA_RETRY_SOURCE, now + self.retry)

    def mark_failed(self, ticket: frozenset, now: float) -> None:
        for source in ticket:
            self._pending.setdefault(source, now + self.retry)
        if self._next_daily is None and "daily" not in ticket:
            self._next_daily = now + self.daily

    def seconds_until_due(self, now: float) -> float:
        times = list(self._pending.values())
        if self._next_daily is not None:
            times.append(self._next_daily)
        if not times:
            return IDLE_WAIT_SECONDS
        return max(MIN_WAIT_SECONDS, min(times) - now)


def source_label(ticket: frozenset) -> str:
    return "+".join(sorted(ticket))
