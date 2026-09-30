"""HA adapter in STEP 7P: subscriptions, event scheduling, snapshot sweeps."""

import asyncio

import pytest

from src.adapters.ha import REGISTRY_EVENTS, HAAdapter
from src.sweep_scheduler import SweepScheduler
from tests.ha_payloads import full_home


class FakeWS:
    def __init__(self, replies):
        self.sent, self.replies = [], list(replies)

    async def send_json(self, data):
        self.sent.append(data)

    async def receive_str(self):
        import json
        return json.dumps(self.replies.pop(0))


def _adapter(scheduler=None):
    return HAAdapter(token="t", ws_url="ws://x", scheduler=scheduler or SweepScheduler())


def test_subscribes_to_device_and_entity_registry_events():
    adapter = _adapter()
    ws = FakeWS([{"type": "result", "success": True}] * 2)
    asyncio.run(adapter._subscribe(ws))
    assert [m["event_type"] for m in ws.sent] == list(REGISTRY_EVENTS)
    assert len({m["id"] for m in ws.sent}) == 2


def test_subscription_failure_raises():
    ws = FakeWS([{"type": "result", "success": False}])
    with pytest.raises(RuntimeError):
        asyncio.run(_adapter()._subscribe(ws))


@pytest.mark.parametrize("event_type", REGISTRY_EVENTS)
def test_registry_event_only_schedules_a_sweep(event_type):
    adapter = _adapter()
    adapter._ws_command = None  # any read here would crash the test
    asyncio.run(adapter._handle_message({"type": "event", "event": {
        "event_type": event_type, "data": {"action": "update", "device_id": "d"}}}))
    assert adapter.scheduler.take(10**9) == {event_type}


def test_other_events_are_ignored():
    adapter = _adapter()
    asyncio.run(adapter._handle_message({"type": "event", "event": {
        "event_type": "state_changed", "data": {"entity_id": "sensor.x"}}}))
    assert adapter.scheduler.take(10**9) is None


def _with_replies(adapter, replies):
    async def fake(command_type, strict=False):
        reply = replies[command_type]
        if reply is None and strict:
            raise RuntimeError(f"{command_type} failed")
        return reply or []
    adapter._ws_command = fake


def test_due_sweep_passes_full_snapshot_to_callback():
    devices, entities, states = full_home()
    adapter = _adapter()
    _with_replies(adapter, {"config/device_registry/list": devices,
                            "config/entity_registry/list": entities,
                            "get_states": states})
    calls = []

    async def on_snapshot(source, d, e, s):
        calls.append((source, d, e, s))

    adapter.on_snapshot = on_snapshot
    adapter.scheduler.on_connected(0)
    asyncio.run(adapter._run_due_sweep())
    assert calls == [("startup", devices, entities, states)]
    assert adapter.scheduler.take(0) is None


def test_failed_read_retries_and_never_calls_callback():
    adapter = _adapter(SweepScheduler(retry=0.0))
    _with_replies(adapter, {"config/device_registry/list": [],
                            "config/entity_registry/list": [],
                            "get_states": None})     # HA returned an error
    called = []

    async def on_snapshot(*args):
        called.append(args)

    adapter.on_snapshot = on_snapshot
    adapter.scheduler.on_connected(0)
    asyncio.run(adapter._run_due_sweep())
    assert called == []
    assert adapter.scheduler.take(10**9) == {"startup"}  # retried


def test_callback_failure_is_retried():
    devices, entities, states = full_home()
    adapter = _adapter(SweepScheduler(retry=0.0))
    _with_replies(adapter, {"config/device_registry/list": devices,
                            "config/entity_registry/list": entities,
                            "get_states": states})

    async def on_snapshot(*args):
        raise ValueError("db locked")

    adapter.on_snapshot = on_snapshot
    adapter.scheduler.on_connected(0)
    asyncio.run(adapter._run_due_sweep())
    assert adapter.scheduler.take(10**9) == {"startup"}


def test_strict_command_raises_on_error_result():
    adapter = _adapter()
    adapter.ws = FakeWS([{"id": 2, "type": "result", "success": False,
                          "error": {"code": "unknown_command"}}])
    with pytest.raises(RuntimeError):
        asyncio.run(adapter._ws_command("get_states", strict=True))


class _Msg:
    def __init__(self, type_, data=None):
        self.type, self.data = type_, data


class ServeWS:
    """receive(): timeout, one registry event, then close."""

    def __init__(self):
        import json
        import aiohttp
        self.script = [
            asyncio.TimeoutError(),
            _Msg(aiohttp.WSMsgType.TEXT, json.dumps({"type": "event", "event": {
                "event_type": "entity_registry_updated", "data": {"action": "create"}}})),
            _Msg(aiohttp.WSMsgType.CLOSED),
        ]
        self.timeouts = []

    async def receive(self, timeout=None):
        self.timeouts.append(timeout)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_serve_runs_startup_sweep_then_schedules_event_and_stops_on_close():
    devices, entities, states = full_home()
    adapter = _adapter(SweepScheduler(debounce=30))
    _with_replies(adapter, {"config/device_registry/list": devices,
                            "config/entity_registry/list": entities,
                            "get_states": states})
    sources = []

    async def on_snapshot(source, *args):
        sources.append(source)

    adapter.on_snapshot = on_snapshot
    adapter.scheduler.on_connected(0)
    ws = ServeWS()
    asyncio.run(adapter._serve(ws))
    assert sources == ["startup"]                      # event debounced, not yet due
    assert ws.timeouts[0] > 86000                      # idle until the daily sweep
    assert adapter.scheduler.take(10**12) == {"entity_registry_updated"}


def test_authenticate():
    ws = FakeWS([{"type": "auth_required"}, {"type": "auth_ok"}])
    asyncio.run(_adapter()._authenticate(ws))
    assert ws.sent == [{"type": "auth", "access_token": "t"}]
    with pytest.raises(RuntimeError):
        asyncio.run(_adapter()._authenticate(
            FakeWS([{"type": "auth_required"}, {"type": "auth_invalid"}])))


def test_ws_command_dispatches_interleaved_events_and_returns_result():
    adapter = _adapter()
    adapter.ws = FakeWS([
        {"type": "event", "event": {"event_type": "device_registry_updated", "data": {}}},
        {"id": 2, "type": "result", "success": True, "result": [{"id": "d"}]},
    ])
    assert asyncio.run(adapter._ws_command("config/device_registry/list")) == [{"id": "d"}]
    assert adapter.scheduler.take(10**12) == {"device_registry_updated"}


def test_subscribe_tolerates_events_before_the_second_result():
    adapter = _adapter()
    ws = FakeWS([
        {"type": "result", "success": True},
        {"type": "event", "event": {"event_type": "device_registry_updated", "data": {}}},
        {"type": "result", "success": True},
    ])
    asyncio.run(adapter._subscribe(ws))
    assert adapter.scheduler.take(10**12) == {"device_registry_updated"}


def test_hanging_snapshot_read_times_out_and_forces_reconnect(monkeypatch):
    import src.adapters.ha as ha_module
    monkeypatch.setattr(ha_module, "SNAPSHOT_TIMEOUT_SECONDS", 0.05)
    adapter = _adapter(SweepScheduler(retry=0.0))

    async def hang(command_type, strict=False):
        await asyncio.sleep(10)

    adapter._ws_command = hang
    adapter.on_snapshot = None
    adapter.scheduler.on_connected(0)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(adapter._run_due_sweep())
    assert adapter.scheduler.take(10**12) == {"startup"}


def test_missing_metadata_count_from_callback_schedules_retry():
    devices, entities, states = full_home()
    adapter = _adapter(SweepScheduler(retry=0.0))
    _with_replies(adapter, {"config/device_registry/list": devices,
                            "config/entity_registry/list": entities,
                            "get_states": states})

    async def on_snapshot(*args):
        return 2

    adapter.on_snapshot = on_snapshot
    adapter.scheduler.on_connected(0)
    asyncio.run(adapter._run_due_sweep())
    assert adapter.scheduler.take(10**12) == {"retry_missing_metadata"}
