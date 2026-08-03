from pathlib import Path
from typing import Dict, Optional
import asyncio
import os
from datetime import datetime, timezone
from uuid import uuid4

from src.storage import Storage
from src.classifiers.energy import EnergyMeterClassifier
from src.classifiers.environmental import EnvironmentalSensorClassifier
from src.classifiers.motion import MotionSensorClassifier

CORE_BUILD = "2026-08-01-step2-ha-app"


class CognitiveCore:
    """Main reasoning engine."""

    def __init__(self, knowledge_path: Path = Path("data")):
        self.knowledge_path = knowledge_path
        db_path = Path(os.getenv("DB_PATH", str(knowledge_path / "core.db")))
        self.storage = Storage(db_path)
        self.classifiers = {}
        self.adapters = {}

    async def start(self):
        print(f"[CORE] Starting Cognitive Core 0.1... (Build: {CORE_BUILD})")
        await self.load_classifiers()
        await self.load_adapters()
        print("[CORE] Ready")

    async def load_classifiers(self):
        self.classifiers["energy_meter"] = EnergyMeterClassifier()
        self.classifiers["environmental_sensor"] = EnvironmentalSensorClassifier()
        self.classifiers["motion_sensor"] = MotionSensorClassifier()
        print(f"[CORE] Loaded {len(self.classifiers)} classifiers")

    async def load_adapters(self):
        from src.adapters.ha import HAAdapter

        token = os.getenv("SUPERVISOR_TOKEN") or os.getenv("HA_TOKEN", "")
        ws_url = os.getenv("HA_WS_URL")
        host = os.getenv("HA_HOST", "home_assistant")

        if not token:
            raise RuntimeError("No Home Assistant token available")

        self.adapters["ha"] = HAAdapter(host=host, token=token, ws_url=ws_url)
        print(f"[CORE] Loaded {len(self.adapters)} adapters")

    async def on_device_detected(self, source: str, device: Dict):
        print(f"[CORE] Device detected: {device.get('name')}")

        hypothesis = await self.classify_device(device, source)
        if not hypothesis:
            print("[CORE] No classifier matched")
            return

        print(f"[CORE] Hypothesis: {hypothesis['category']} ({hypothesis['confidence']})")

        asset_id = self.storage.save_asset({
            "id": f"asset_{uuid4().hex[:8]}",
            "lifecycle_discovered_at": datetime.now(timezone.utc).isoformat(),
            "source_device_id": device.get("id"),
            "source_adapter": source,
            "name": device.get("name"),
            "hypothesis": hypothesis,
        })

        print(f"[CORE] Asset saved: {asset_id}")

        if hypothesis["confidence"] < 0.95:
            self.storage.add_to_review_queue(asset_id)
            print(f"[CORE] Added to review queue: {asset_id}")

    async def classify_device(self, device: Dict, source: str = "unknown") -> Optional[Dict]:
        best_hypothesis = None
        best_score = 0.0
        observation_group_id = f"obsg_{uuid4().hex[:12]}"

        for classifier_name, classifier in self.classifiers.items():
            result = await classifier.classify(device)
            print(f"[CORE] Result from {classifier_name}: {result}")

            if result:
                self.storage.log_classification_observation({
                    "observation_group_id": observation_group_id,
                    "device_id": device.get("id") or "unknown",
                    "classifier_name": classifier_name,
                    "hypothesis_category": result.get("category") or "unknown",
                    "hypothesis_confidence": result.get("confidence", 0.0),
                    "hypothesis_reasoning": result.get("reasoning", ""),
                    "device_name": device.get("name") or "",
                    "device_model": device.get("model") or "",
                    "device_manufacturer": device.get("manufacturer") or "",
                    "device_source_adapter": source,
                    "device_entity_count": len(device.get("entities") or []),
                })

                if result["confidence"] > best_score:
                    best_hypothesis = result
                    best_score = result["confidence"]

        return best_hypothesis
