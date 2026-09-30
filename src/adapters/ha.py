import asyncio
import aiohttp
import json
import time
from typing import Awaitable, Callable, Optional

from src.observation_input import build_inputs
from src.sweep_scheduler import SweepScheduler, source_label

# STEP 7P: registry changes that can change a device's classifier input.
REGISTRY_EVENTS = ("device_registry_updated", "entity_registry_updated")

SnapshotCallback = Callable[[str, list, list, list], Awaitable[Optional[int]]]

# A registry read that takes longer than this is treated as a broken
# connection: the partially read response stream cannot be trusted, so the
# adapter reconnects instead of continuing on the same socket.
SNAPSHOT_TIMEOUT_SECONDS = 60.0


class HAAdapter:
    """Home Assistant WebSocket adapter.

    STEP 7P: instead of handing single devices to the core, the adapter
    reads full registry snapshots (device list, entity list, states) when a
    sweep is due and passes them to the core's snapshot callback. When a
    sweep is due is decided by SweepScheduler (startup/reconnect, daily,
    debounced registry events).

    All WebSocket reads happen on the listen() task: sweeps run inline
    between messages, never concurrently, because aiohttp allows only one
    reader per connection.
    """

    def __init__(
        self,
        host: str = "home_assistant",
        token: Optional[str] = None,
        ws_url: Optional[str] = None,
        scheduler: Optional[SweepScheduler] = None,
    ):
        self.host = host
        self.token = token or ""
        self.ws_url = ws_url or f"ws://{self.host}:8123/api/websocket"
        self.on_snapshot: Optional[SnapshotCallback] = None
        self.scheduler = scheduler or SweepScheduler()
        self.ws = None
        self._msg_id = 1
        self._lock = asyncio.Lock()

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def listen(self, on_snapshot: SnapshotCallback):
        """Keep a connection open and run registry sweeps when due."""
        self.on_snapshot = on_snapshot

        while True:
            try:
                print(f"[HA] Connecting to {self.ws_url}...")

                async with aiohttp.ClientSession() as session:
                    # max_msg_size=0: get_states on a large instance can
                    # exceed aiohttp's 4 MB default.
                    async with session.ws_connect(self.ws_url, max_msg_size=0) as ws:
                        self.ws = ws
                        print("[HA] Connected")

                        await self._authenticate(ws)
                        await self._subscribe(ws)
                        self.scheduler.on_connected(time.monotonic())
                        await self._serve(ws)

            except Exception as exc:
                print(f"[HA] Connection error: {exc}")
            await asyncio.sleep(5)

    async def _serve(self, ws):
        """Alternate between due sweeps and incoming messages until closed."""
        while True:
            await self._run_due_sweep()
            try:
                msg = await ws.receive(
                    timeout=self.scheduler.seconds_until_due(time.monotonic()))
            except asyncio.TimeoutError:
                continue
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._handle_message(json.loads(msg.data))
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                              aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                print(f"[HA] Connection closed ({msg.type})")
                return

    async def _run_due_sweep(self):
        ticket = self.scheduler.take(time.monotonic())
        if not ticket:
            return
        source = source_label(ticket)
        try:
            devices, entities, states = await asyncio.wait_for(
                self.fetch_snapshot(), SNAPSHOT_TIMEOUT_SECONDS)
            missing_metadata = await self.on_snapshot(source, devices, entities, states)
            self.scheduler.mark_done(time.monotonic(), missing_metadata or 0)
        except Exception as exc:
            print(f"[HA] Sweep ({source}) failed, will retry: {exc!r}")
            self.scheduler.mark_failed(ticket, time.monotonic())
            if isinstance(exc, (aiohttp.ClientError, ConnectionError,
                                asyncio.TimeoutError)):
                raise

    async def fetch_snapshot(self) -> tuple:
        """(devices, entities, states); raises if any read fails."""
        devices = await self._ws_command("config/device_registry/list", strict=True)
        entities = await self._ws_command("config/entity_registry/list", strict=True)
        states = await self._ws_command("get_states", strict=True)
        return devices, entities, states

    async def _authenticate(self, ws):
        auth_required = json.loads(await ws.receive_str())
        if auth_required.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected message before auth: {auth_required}")

        await ws.send_json({"type": "auth", "access_token": self.token})
        response = json.loads(await ws.receive_str())

        if response.get("type") == "auth_ok":
            print("[HA] Authenticated")
        else:
            raise RuntimeError(f"Authentication failed: {response}")

    async def _subscribe(self, ws):
        for event_type in REGISTRY_EVENTS:
            await ws.send_json({
                "id": self._next_id(),
                "type": "subscribe_events",
                "event_type": event_type,
            })
            # Events of an earlier subscription may arrive before this result.
            while True:
                response = json.loads(await ws.receive_str())
                if response.get("type") == "event":
                    await self._handle_message(response)
                    continue
                break
            if response.get("type") != "result" or not response.get("success"):
                raise RuntimeError(f"Subscription failed: {response}")
            print(f"[HA] Subscribed to {event_type}")

    async def _ws_command(self, command_type: str, strict: bool = False) -> list:
        async with self._lock:
            req_id = self._next_id()
            await self.ws.send_json({"id": req_id, "type": command_type})

            while True:
                msg = await self.ws.receive_str()
                data = json.loads(msg)

                if data.get("id") == req_id and data.get("type") == "result":
                    if data.get("success"):
                        return data.get("result", [])
                    print(f"[HA] Command {command_type} failed: {data.get('error')}")
                    if strict:
                        raise RuntimeError(f"{command_type} failed: {data.get('error')}")
                    return []

                if data.get("type") == "event":
                    await self._handle_message(data)

    async def _handle_message(self, message: dict):
        """Registry events only schedule a sweep; they never read here."""
        if message.get("type") != "event":
            return

        event = message.get("event", {})
        event_type = event.get("event_type")
        data = event.get("data", {})
        print(f"[HA DEBUG] event {event_type} action={data.get('action')} "
              f"device_id={data.get('device_id')} entity_id={data.get('entity_id')}")

        if event_type in REGISTRY_EVENTS:
            self.scheduler.on_event(event_type, time.monotonic())

    async def _enrich_device(self, device: dict) -> dict:
        """Classifier view of one device (entities as passed to classifiers).

        Not used by the sweep path; kept for scripts/ha_input_probe.py, which
        reports what the deployed adapter passes to classifiers. For a
        skipped device (no enabled entities / missing metadata) the entity
        list is empty.
        """
        try:
            entities = await self._ws_command("config/entity_registry/list")
            states = await self._ws_command("get_states")
            device_input = build_inputs([device], entities, states)[0]
            device["entities"] = (device_input.snapshot or {}).get("entities", [])
        except Exception as exc:
            print(f"[HA] Error enriching device: {exc}")
        return device
