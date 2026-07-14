# src/adapters/ha.py

import asyncio
import aiohttp
import json
from typing import Callable

class HAAdapter:
    """Home Assistant WebSocket adapter"""
    
    def __init__(self, host: str = "home_assistant", token: str = None):
        self.host = host
        self.token = token or "your_token_here"
        self.callback = None
        self.ws = None
        self._msg_id = 1
        self._lock = asyncio.Lock()
    
    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id
    
    async def listen(self, callback: Callable):
        """Listen to HA device registry updates"""
        self.callback = callback
        
        ws_url = f"ws://{self.host}:8123/api/websocket"
        
        while True:
            try:
                print(f"[HA] Connecting to {ws_url}...")
                
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(ws_url) as ws:
                        self.ws = ws
                        print("[HA] Connected")
                        
                        await self._authenticate(ws)
                        await self._subscribe(ws)
                        
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                await self._handle_message(data)
            
            except Exception as e:
                print(f"[HA] Connection error: {e}")
                await asyncio.sleep(5)
    
    async def _authenticate(self, ws):
        """Authenticate to HA"""
        auth_required = json.loads(await ws.receive_str())
        if auth_required.get('type') != 'auth_required':
            raise Exception(f"Unexpected message before auth: {auth_required}")

        await ws.send_json({
            "type": "auth",
            "access_token": self.token
        })
        
        response = json.loads(await ws.receive_str())
        
        if response.get('type') == 'auth_ok':
            print("[HA] Authenticated")
        else:
            raise Exception(f"Authentication failed: {response}")
    
    async def _subscribe(self, ws):
        """Subscribe to device registry updates"""
        await ws.send_json({
            "id": 1,
            "type": "subscribe_events",
            "event_type": "device_registry_updated"
        })
        
        response = json.loads(await ws.receive_str())
        print(f"[HA] Subscribed to device_registry_updated")
    
    async def _ws_command(self, command_type: str) -> list:
        """Send a WS command and wait for its matching result, ignoring other messages"""
        async with self._lock:
            req_id = self._next_id()
            await self.ws.send_json({
                "id": req_id,
                "type": command_type
            })
            
            while True:
                msg = await self.ws.receive_str()
                data = json.loads(msg)
                
                if data.get('id') == req_id and data.get('type') == 'result':
                    if data.get('success'):
                        return data.get('result', [])
                    else:
                        print(f"[HA] Command {command_type} failed: {data.get('error')}")
                        return []
                
                if data.get('type') == 'event':
                    await self._handle_message(data)
    
    async def _handle_message(self, message: dict):
        """Handle incoming message"""
        
        if message.get('type') != 'event':
            return
        
        print(f"[HA DEBUG] Raw event: {message}")
        
        event_data = message.get('event', {}).get('data', {})
        action = event_data.get('action')
        device_id = event_data.get('device_id')
        
        if action == 'create':
            device = await self._get_device(device_id)
            
            if device and self.callback:
                await self.callback('ha', device)
    
    async def _get_device(self, device_id: str) -> dict:
        """Get device details from HA via WebSocket"""
        print(f"[HA DEBUG] _get_device called for {device_id}")
        
        try:
            devices = await self._ws_command("config/device_registry/list")
            print(f"[HA DEBUG] Got {len(devices)} devices via WS")
            
            for device in devices:
                if device.get('id') == device_id:
                    print(f"[HA DEBUG] Found matching device")
                    return await self._enrich_device(device)
        
        except Exception as e:
            print(f"[HA] Error getting device: {e}")
        
        print(f"[HA DEBUG] _get_device returning None")
        return None
    
    async def _enrich_device(self, device: dict) -> dict:
        """Add entity information to device via WebSocket"""
        
        try:
            entities = await self._ws_command("config/entity_registry/list")
            
            device_entities = [
                {
                    'name': e.get('name') or e.get('original_name'),
                    'domain': e.get('platform') or (e.get('entity_id', '').split('.')[0] if e.get('entity_id') else None),
                    'entity_id': e.get('entity_id')
                }
                for e in entities
                if e.get('device_id') == device.get('id')
            ]
            
            device['entities'] = device_entities
        
        except Exception as e:
            print(f"[HA] Error enriching device: {e}")
        
        return device