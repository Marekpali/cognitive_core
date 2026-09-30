"""HAAdapter against a REAL aiohttp WebSocket server (no fakes of aiohttp).

Exercises the library behaviour the adapter depends on - receive(timeout),
max_msg_size, close handling, reconnect - on the pinned aiohttp version.
"""

import asyncio
import json

import aiohttp
from aiohttp import web

import src.adapters.ha as ha_module
from src.adapters.ha import HAAdapter
from src.sweep_scheduler import SweepScheduler
from tests.ha_payloads import full_home


class FakeHA:
    """Minimal Home Assistant WebSocket API: auth, subscribe, 3 reads."""

    def __init__(self, big_states=False, hang_on=None, close_after_first_sweep=False):
        self.devices, self.entities, self.states = full_home()
        if big_states:  # > aiohttp's 4 MB default max_msg_size
            self.states = self.states + [
                {"entity_id": f"sensor.filler_{i}", "state": "x",
                 "attributes": {"blob": "y" * 10_000}} for i in range(500)]
        self.hang_on = hang_on
        self.close_after_first_sweep = close_after_first_sweep
        self.connections = 0
        self.sweeps_served = 0

    async def handler(self, request):
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)
        self.connections += 1
        await ws.send_json({"type": "auth_required"})
        await ws.receive_json()
        await ws.send_json({"type": "auth_ok"})
        async for msg in ws:
            data = json.loads(msg.data)
            kind = data["type"]
            if kind == "subscribe_events":
                await ws.send_json({"id": data["id"], "type": "result", "success": True})
                continue
            if kind == self.hang_on and self.connections == 1:
                continue  # never answer on the first connection
            result = {"config/device_registry/list": self.devices,
                      "config/entity_registry/list": self.entities,
                      "get_states": self.states}[kind]
            await ws.send_json({"id": data["id"], "type": "result",
                                "success": True, "result": result})
            if kind == "get_states":
                self.sweeps_served += 1
                if self.sweeps_served == 1:
                    await ws.send_json({"type": "event", "event": {
                        "event_type": "entity_registry_updated",
                        "data": {"action": "create", "entity_id": "sensor.new"}}})
                if self.close_after_first_sweep and self.connections == 1:
                    await asyncio.sleep(0.3)  # let the event's sweep not start yet
                    await ws.close()
                    return ws
        return ws


async def _run(fake: FakeHA, want: int, timeout: float = 8.0) -> list:
    app = web.Application()
    app.router.add_get("/ws", fake.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    seen = []
    done = asyncio.Event()

    async def on_snapshot(source, devices, entities, states):
        seen.append((source, len(devices), len(states)))
        if len(seen) >= want:
            done.set()
        return 0

    adapter = HAAdapter(token="t", ws_url=f"ws://127.0.0.1:{port}/ws",
                        scheduler=SweepScheduler(debounce=0.2, retry=0.2))
    task = asyncio.create_task(adapter.listen(on_snapshot))
    try:
        await asyncio.wait_for(done.wait(), timeout)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runner.cleanup()
    return seen


def test_startup_sweep_then_debounced_event_sweep():
    seen = asyncio.run(_run(FakeHA(), want=2))
    assert [s[0] for s in seen] == ["startup", "entity_registry_updated"]
    assert seen[0][1] == 5


def test_get_states_larger_than_4_mb_is_received():
    fake = FakeHA(big_states=True)
    seen = asyncio.run(_run(fake, want=1))
    assert seen[0][2] > 500
    assert fake.connections == 1


def test_closed_connection_reconnects_and_sweeps_again(monkeypatch):
    monkeypatch.setattr(ha_module, "RECONNECT_DELAY_SECONDS", 0.1)
    fake = FakeHA(close_after_first_sweep=True)
    seen = asyncio.run(_run(fake, want=2))
    assert seen[0][0] == "startup"
    assert "reconnect" in seen[1][0]
    assert fake.connections == 2


def test_hanging_read_times_out_and_reconnects(monkeypatch):
    monkeypatch.setattr(ha_module, "SNAPSHOT_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(ha_module, "RECONNECT_DELAY_SECONDS", 0.1)
    fake = FakeHA(hang_on="get_states")
    seen = asyncio.run(_run(fake, want=1))
    assert fake.connections == 2
    assert "startup" in seen[0][0] or "reconnect" in seen[0][0]


def test_aiohttp_receive_timeout_zero_waits_for_default():
    """Documents the library behaviour behind MIN_WAIT_SECONDS: timeout=0 is
    treated as 'no timeout', so the adapter must never pass 0."""
    import inspect
    source = inspect.getsource(aiohttp.ClientWebSocketResponse.receive)
    assert "timeout or self._" in source.replace("\n", " ")
