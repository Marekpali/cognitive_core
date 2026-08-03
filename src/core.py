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
from src.ha_sensor import update_pending_reviews

CORE_BUILD = "2026-08-01-step2-ha-app"


class CognitiveCore:
    """Main reasoning engine."""

    def __init__(self, knowledge_path: Path = Path("data")):
        self.knowledge_path = knowledge_path
        db_path = Path(os.getenv("DB_PATH", str(knowledge_path / "core.db")))
        self.storage = Storage(db_path)
        self.classifiers = {}
        self.adapters = {}
        # STEP 4A.2: holds references to fire-and-forget sensor-push
        # tasks so they aren't garbage-collected mid-execution (a
        # documented asyncio pitfall - create_task() alone does not
        # guarantee the task survives if nothing else references it).
        self._background_tasks: set = set()

    def _fire_and_forget(self, coro) -> None:
        """Schedule a coroutine without awaiting it, safely.

        Keeps a reference in self._background_tasks until the task
        completes, then discards it, so it can't be garbage-collected
        prematurely.
        """
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def start(self):
        print(f"[CORE] Starting Cognitive Core 0.1... (Build: {CORE_BUILD})")
        await self.load_classifiers()
        await self.load_adapters()
        print("[CORE] Ready")
        # STEP 4A.2: publish initial sensor state on startup.
        # asyncio.to_thread: see on_device_detected() below for why
        # this is not called directly.
        self._fire_and_forget(asyncio.to_thread(update_pending_reviews, self.storage))

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

        # STEP 4A.2: classify_device() may have logged one or more
        # classification_observations rows (each starts as
        # review_status='pending'), even if no single hypothesis was
        # chosen as "best" below. Refresh the sensor unconditionally,
        # right after classification, so the pending count in Home
        # Assistant reflects reality regardless of which branch runs
        # next.
        #
        # asyncio.to_thread: update_pending_reviews() is a blocking
        # call (sqlite3 count + requests.post to the Supervisor proxy).
        # Calling it directly would stall the event loop for the
        # duration of the HTTP request - including the HA adapter's
        # listen() task, which needs to keep processing incoming
        # WebSocket events. Under a slow or unresponsive Supervisor,
        # a direct call could block device detection for up to
        # REQUEST_TIMEOUT_SECONDS. Running it in a thread keeps this
        # handler responsive regardless of how long the HTTP call takes.
        self._fire_and_forget(asyncio.to_thread(update_pending_reviews, self.storage))

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
