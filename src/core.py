# src/core.py

import asyncio
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional
from uuid import uuid4

from src.storage import Storage

class CognitiveCore:
    """Main reasoning engine"""
    
    def __init__(self, knowledge_path: Path = Path("data")):
        self.knowledge_path = knowledge_path
        self.storage = Storage(knowledge_path / "core.db")
        self.classifiers = {}
        self.adapters = {}
    
    async def start(self):
        """Start engine"""
        print("[CORE] Starting Cognitive Core 0.1...")
        
        await self.load_classifiers()
        await self.load_adapters()
        
        print("[CORE] Ready")
    
    async def load_classifiers(self):
        """Load classifiers"""
        from src.classifiers.energy import EnergyMeterClassifier
        
        self.classifiers['energy_meter'] = EnergyMeterClassifier()
        
        print(f"[CORE] Loaded {len(self.classifiers)} classifiers")
    
    async def load_adapters(self):
        """Load adapters"""
        from src.adapters.mock import MockAdapter
        from src.adapters.ha import HAAdapter
        import os
        
        if os.getenv('USE_MOCK') == 'true':
            self.adapters['mock'] = MockAdapter()
            print("[CORE] Using MOCK adapter")
        else:
            ha_host = os.getenv('HA_HOST', 'home_assistant')
            ha_token = os.getenv('HA_TOKEN', '')
            
            self.adapters['ha'] = HAAdapter(host=ha_host, token=ha_token)
            print("[CORE] Using HA adapter")
        
        print(f"[CORE] Loaded {len(self.adapters)} adapters")
    
    async def on_device_detected(self, source: str, device: Dict):
        """Device detected"""
        
        print(f"\n[CORE] Device detected: {device.get('name')}")
        
        # Classify
        hypothesis = await self.classify_device(device)
        
        if not hypothesis:
            print("[CORE] No classifier matched")
            return
        
        print(f"[CORE] Hypothesis: {hypothesis['category']} ({hypothesis['confidence']:.0%})")
        
        # Create asset
        asset = {
            'id': f"asset_{hypothesis['category']}_{uuid4().hex[:8]}",
            'name': device.get('name', 'Unknown'),
            'source_device_id': device.get('id'),
            'source_adapter': source,
            'hypothesis': hypothesis,
            'lifecycle_state': 'provisional',
            'lifecycle_discovered_at': datetime.utcnow().isoformat() + 'Z'
        }
        
        # Save
        asset_id = self.storage.save_asset(asset)
        
        # Add to review if needed
        if hypothesis['confidence'] < 0.95:
            self.storage.add_to_review_queue(asset_id)
            print("[CORE] Added to review queue")
    
    async def classify_device(self, device: Dict) -> Optional[Dict]:
        """Classify device"""
        
        best_hypothesis = None
        best_score = 0
        
        for classifier_name, classifier in self.classifiers.items():
            hypothesis = await classifier.classify(device)
            
            if hypothesis and hypothesis['confidence'] > best_score:
                best_score = hypothesis['confidence']
                best_hypothesis = hypothesis
        
        return best_hypothesis
