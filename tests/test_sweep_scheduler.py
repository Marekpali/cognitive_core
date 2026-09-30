"""When sweeps run (ADR 7.3): startup/reconnect, daily, debounced events, retry."""

from src.sweep_scheduler import (
    MAX_METADATA_RETRIES, METADATA_RETRY_SOURCE, MIN_WAIT_SECONDS, SweepScheduler,
    source_label,
)


def _sched():
    return SweepScheduler(debounce=30, daily=86400, retry=300)


def test_first_connect_is_startup_then_reconnect():
    s = _sched()
    s.on_connected(0)
    assert s.take(0) == {"startup"}
    s.mark_done(1)
    s.on_connected(100)
    assert s.take(100) == {"reconnect"}


def test_nothing_due_without_a_trigger():
    s = _sched()
    assert s.take(10**9) is None


def test_event_burst_collapses_into_one_debounced_sweep():
    s = _sched()
    s.on_event("entity_registry_updated", 0)
    s.on_event("entity_registry_updated", 10)
    s.on_event("device_registry_updated", 20)
    assert s.take(29) is None
    ticket = s.take(30)
    assert ticket == {"entity_registry_updated", "device_registry_updated"}
    assert source_label(ticket) == "device_registry_updated+entity_registry_updated"
    assert s.take(31) is None


def test_debounce_is_not_sliding():
    s = _sched()
    for t in range(0, 30, 5):
        s.on_event("entity_registry_updated", t)
    assert s.take(30) == {"entity_registry_updated"}


def test_event_joins_an_earlier_scheduled_sweep():
    s = _sched()
    s.on_connected(0)
    s.on_event("device_registry_updated", 0)
    assert s.take(0) == {"startup", "device_registry_updated"}


def test_event_during_a_running_sweep_is_not_lost():
    s = _sched()
    s.on_connected(0)
    ticket = s.take(0)                 # sweep starts
    s.on_event("device_registry_updated", 5)   # arrives mid-sweep
    s.mark_done(6)
    assert ticket == {"startup"}
    assert s.take(35) == {"device_registry_updated"}


def test_daily_after_last_completed_sweep():
    s = _sched()
    s.on_connected(0)
    s.take(0)
    s.mark_done(10)
    assert s.take(86409) is None
    assert s.take(86410) == {"daily"}
    assert s.seconds_until_due(86411) == 60.0  # idle until the next trigger
    s.mark_done(86411)
    assert s.seconds_until_due(86411) == 86400


def test_failed_sweep_is_retried_with_its_sources():
    s = _sched()
    s.on_connected(0)
    ticket = s.take(0)
    s.mark_failed(ticket, 1)
    assert s.take(300) is None
    assert s.take(301) == {"startup"}


def test_seconds_until_due_tracks_the_earliest_trigger():
    s = _sched()
    s.on_event("device_registry_updated", 100)
    assert s.seconds_until_due(110) == 20
    assert s.seconds_until_due(200) == MIN_WAIT_SECONDS  # never 0: aiohttp waits forever


def test_event_moves_a_later_retry_earlier():
    s = _sched()
    s.on_connected(0)
    s.mark_failed(s.take(0), 0)                          # startup retry at 300
    s.on_event("device_registry_updated", 10)            # not held back to 300
    assert s.take(39) is None
    assert s.take(40) == {"startup", "device_registry_updated"}
    s.mark_failed(frozenset({"device_registry_updated"}), 40)   # event retry at 340
    s.on_event("device_registry_updated", 50)            # a new event moves it to 80
    assert s.take(79) is None
    assert s.take(80) == {"device_registry_updated"}


def test_missing_metadata_is_retried_a_bounded_number_of_times():
    s = _sched()
    now = 0
    for _ in range(MAX_METADATA_RETRIES):
        s.mark_done(now, missing_metadata=5)
        now += 300
        assert s.take(now) == {METADATA_RETRY_SOURCE}
    s.mark_done(now, missing_metadata=5)
    assert s.take(now + 300) is None                     # budget spent -> daily only
    s.mark_done(now, missing_metadata=0)                 # a clean sweep resets it
    s.mark_done(now, missing_metadata=1)
    assert s.take(now + 300) == {METADATA_RETRY_SOURCE}
