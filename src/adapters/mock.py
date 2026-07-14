# src/adapters/mock.py

import asyncio
from src.adapters.base import Adapter

class MockAdapter(Adapter):
    """Mock adapter for testing"""
    
    async def listen(self, callback):
        """Listen for signals"""
        print("[MOCK] Ready for test signals")
        
        while True:
            await asyncio.sleep(1)
    
    async def send_test_device(self, callback, device: dict):
        """Send test device"""
        print(f"[MOCK] Sending test device: {device['name']}")
        
        await callback('mock', {
            'type': 'device_detected',
            'data': device
        })
