import asyncio
import aiohttp
import json
from typing import Callable, Optional


class HAAdapter:
    """Home Assistant WebSocket adapter."""

    def __init__(
        self,
        host: str = "home_assistant",
        token: Optional[str] = None,
        ws_url: Optional[str] = None,
    ):
        self.host = host
        self.token = token or ""
        self.ws_url = ws_url or f"ws://{self.host}:8123/api/websocket"
        self.callback = None
        self.ws = None
        self._msg_id = 1
        self._lock = asyncio.Lock()

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def listen(self, callback: Callable):
        """Listen to Home Assistant device registry updates."""
        self.callback = callback

        while True:
            try:
                print(f"[HA] Connecting to {self.ws_url}...")

                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.ws_url) as ws:
                        self.ws = ws
                        print("[HA] Connected")

                        await self._authenticate(ws)
                        await self._subscribe(ws)

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                await self._handle_message(data)

            except Exception as exc:
                print(f"[HA] Connection error: {exc}")
                await asyncio.sleep(5)

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
        await ws.send_json({
            "id": 1,
            "type": "subscribe_events",
            "event_type": "device_registry_updated",
        })
        response = json.loads(await ws.receive_str())
        if response.get("type") != "result" or not response.get("success"):
            raise RuntimeError(f"Subscription failed: {response}")
        print("[HA] Subscribed to device_registry_updated")

    async def _ws_command(self, command_type: str) -> list:
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
                    return []

                if data.get("type") == "event":
                    await self._handle_message(data)

    async def _handle_message(self, message: dict):
        if message.get("type") != "event":
            return

        event_data = message.get("event", {}).get("data", {})
        action = event_data.get("action")
        device_id = event_data.get("device_id")
        
        print(f"[HA DEBUG] event action={action} device_id={device_id}")

        if action in {"create", "update"}:
            device = await self._get_device(device_id)
            if device and self.callback:
                await self.callback("ha", device)

    async def _get_device(self, device_id: str) -> Optional[dict]:
        try:
            devices = await self._ws_command("config/device_registry/list")
            for device in devices:
                if device.get("id") == device_id:
                    return await self._enrich_device(device)
        except Exception as exc:
            print(f"[HA] Error getting device: {exc}")
        return None

    async def _enrich_device(self, device: dict) -> dict:
        try:
            entities = await self._ws_command("config/entity_registry/list")
            device["entities"] = [
                {
                    "name": entity.get("name") or entity.get("original_name"),
                    "domain": entity.get("platform")
                    or (
                        entity.get("entity_id", "").split(".")[0]
                        if entity.get("entity_id")
                        else None
                    ),
                    "entity_id": entity.get("entity_id"),
                }
                for entity in entities
                if entity.get("device_id") == device.get("id")
            ]
        except Exception as exc:
            print(f"[HA] Error enriching device: {exc}")
        return device
