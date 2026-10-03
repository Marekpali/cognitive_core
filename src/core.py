from pathlib import Path
import asyncio
import os

from src.storage import Storage
from src.classifiers.energy import EnergyMeterClassifier
from src.classifiers.environmental import EnvironmentalSensorClassifier
from src.classifiers.motion import MotionSensorClassifier
from src.ha_sensor import update_pending_reviews
from src.options import read_observation_mode, read_precedent_mode
from src.sweep import ObservationSweep

CORE_BUILD = "2026-10-03-step7a"


class CognitiveCore:
    """Main reasoning engine."""

    def __init__(self, knowledge_path: Path = Path("data")):
        self.knowledge_path = knowledge_path
        db_path = Path(os.getenv("DB_PATH", str(knowledge_path / "core.db")))
        self.precedent_mode = read_precedent_mode()
        self.storage = Storage(db_path, precedent_mode=self.precedent_mode)
        self.classifiers = {}
        self.adapters = {}
        self.observation_mode = read_observation_mode()
        self.sweep = None
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
        if self.precedent_mode == "shadow":
            # STEP 7a: the layer's cut-off, written once, before the adapter
            # connects and any observation is written in this mode.
            started_at = self.storage.ensure_precedent_layer_started()
            print(f"[PRECEDENT] layer started at {started_at}")
        await self.load_classifiers()
        await self.load_adapters()
        print("[CORE] Ready")
        # STEP 4A.2: publish initial sensor state on startup.
        # asyncio.to_thread: see on_snapshot() below for why
        # this is not called directly.
        self._fire_and_forget(asyncio.to_thread(update_pending_reviews, self.storage))

    async def load_classifiers(self):
        self.classifiers["energy_meter"] = EnergyMeterClassifier()
        self.classifiers["environmental_sensor"] = EnvironmentalSensorClassifier()
        self.classifiers["motion_sensor"] = MotionSensorClassifier()
        print(f"[CORE] Loaded {len(self.classifiers)} classifiers")
        self.sweep = ObservationSweep(self.storage, self.classifiers, self.observation_mode)

    async def load_adapters(self):
        from src.adapters.ha import HAAdapter

        token = os.getenv("SUPERVISOR_TOKEN") or os.getenv("HA_TOKEN", "")
        ws_url = os.getenv("HA_WS_URL")
        host = os.getenv("HA_HOST", "home_assistant")

        if not token:
            raise RuntimeError("No Home Assistant token available")

        self.adapters["ha"] = HAAdapter(host=host, token=token, ws_url=ws_url)
        print(f"[CORE] Loaded {len(self.adapters)} adapters")

    async def on_snapshot(self, source: str, devices: list, entities: list,
                          states: list) -> int:
        """STEP 7P: run one gated sweep over a full registry snapshot.

        Called by the HA adapter for every due sweep (startup/reconnect,
        daily, debounced registry events). See src/sweep.py. Returns the
        number of devices skipped for missing metadata, which the adapter's
        scheduler uses to retry soon (e.g. HA still loading states).
        """
        result = await self.sweep.run(source, devices, entities, states)

        # STEP 4A.2: refresh the pending-reviews sensor when this sweep
        # wrote observations (active mode only). asyncio.to_thread because
        # update_pending_reviews() blocks (sqlite3 + HTTP to the Supervisor)
        # and must not stall the adapter's listen() task.
        if result.observations_written:
            self._fire_and_forget(asyncio.to_thread(update_pending_reviews, self.storage))
        return result.counts["skipped_missing_metadata"]
