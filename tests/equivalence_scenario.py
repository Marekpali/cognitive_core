"""A fixed STEP 7P scenario, run as a script against whichever `src` is first
on sys.path, printing a normalised dump of everything STEP 7P writes.

Used by tests/test_7p_equivalence.py to compare the current code with the
deployed 8570f77: same inputs, observations, review cases, fingerprint
gating and sweep results. Deliberately uses only API that exists in both.

    python equivalence_scenario.py <db path>      (env START_LAYER=1: 7a shadow)
"""

import asyncio
import copy
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from src.classifiers.energy import EnergyMeterClassifier
from src.classifiers.environmental import EnvironmentalSensorClassifier
from src.classifiers.motion import MotionSensorClassifier
from src.storage import Storage
from src.sweep import ObservationSweep
from tests.ha_payloads import full_home

TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|\+00:00)")
ID = re.compile(r"\b(obs|inp|case|swp|obsg)_[0-9a-f]{8,12}\b")
TABLES = {
    "classification_inputs": "device_id, fingerprint, classifier_set_version, snapshot_json, "
                             "outcome, matched_classifiers, sweep_id, id",
    "classification_observations": "id, observation_group_id, device_id, classifier_name, "
                                   "hypothesis_category, hypothesis_confidence, "
                                   "hypothesis_reasoning, device_name, device_model, "
                                   "device_manufacturer, device_source_adapter, "
                                   "device_entity_count, input_id, review_status, "
                                   "human_decision, corrected_category, review_reason, "
                                   "reviewed_by",
    "review_cases": "id, device_id, classifier_name, hypothesis_category, status, decision, "
                    "corrected_category, last_observation_id",
    "classification_sweeps": "id, mode, source, classifier_set_version, baseline_sweep_id, "
                             "discovered, skipped_no_entities, skipped_missing_metadata, "
                             "unchanged, classified, no_match, error, "
                             "excluded_disabled_entities, unavailable_entities, devices_json",
    "assets": "id, name",
}


class Exploding:
    """Raises for one device, matches nothing otherwise."""

    def __init__(self, device_id):
        self.device_id = device_id

    async def classify(self, device):
        if device.get("id") == self.device_id:
            raise ValueError("boom")
        return None


def classifiers(extra=None):
    base = {"energy_meter": EnergyMeterClassifier(),
            "environmental_sensor": EnvironmentalSensorClassifier(),
            "motion_sensor": MotionSensorClassifier()}
    return dict(base, **(extra or {}))


def run(storage, mode, snapshot, source, extra=None):
    result = asyncio.run(ObservationSweep(storage, classifiers(extra), mode).run(source, *snapshot))
    return [source, mode, result.counts, result.observations_written,
            {d: e["outcome"] for d, e in sorted(result.devices.items())}]


def changed(index, name):
    devices, entities, states = copy.deepcopy(full_home())
    devices[index]["name"] = name
    return devices, entities, states


def scenario(path: Path) -> list:
    storage = Storage(path)
    if os.getenv("START_LAYER") == "1":
        storage.ensure_precedent_layer_started()
    log = []
    log.append(run(storage, "shadow", full_home(), "startup"))
    log.append(run(storage, "active", full_home(), "startup"))
    log.append(run(storage, "active", full_home(), "entity_registry_updated"))

    # a human decision between sweeps: resolved, repeated, stale, unknown
    cases = storage.get_pending_review_cases()
    first = sorted(cases, key=lambda c: (c["device_id"], c["classifier_name"]))[0]
    log.append(storage.resolve_review_case(first["case_id"], first["observation_id"], "approved"))
    log.append(storage.resolve_review_case(first["case_id"], first["observation_id"], "rejected"))
    log.append(storage.resolve_review_case("case_missing", "obs_missing", "approved"))

    log.append(run(storage, "active", changed(2, "Renamed socket"), "device_registry_updated"))
    log.append(run(storage, "active", changed(0, "Renamed motion"), "device_registry_updated"))
    second = sorted(storage.get_pending_review_cases(),
                    key=lambda c: (c["device_id"], c["classifier_name"]))[0]
    log.append(storage.resolve_review_case(second["case_id"], "obs_not_current", "approved"))
    log.append(storage.resolve_review_case(
        second["case_id"], second["observation_id"], "corrected", corrected_category="occupancy"))

    # classifier error on a changed device, then recovery
    log.append(run(storage, "active", changed(3, "Changed app"), "entity_registry_updated",
                   {"x": Exploding("dev_app")}))
    log.append(run(storage, "active", changed(3, "Changed app"), "entity_registry_updated"))

    # missing metadata
    devices, entities, states = copy.deepcopy(full_home())
    states = [s for s in states if s["entity_id"] != "sensor.desk_lamp_power"]
    log.append(run(storage, "active", (devices, entities, states), "daily"))
    log.append(run(storage, "active", full_home(), "reconnect"))
    storage.connection.close()
    return log


def dump(path: Path) -> dict:
    conn = sqlite3.connect(path)
    out = {table: [list(row) for row in conn.execute(
        f"SELECT {columns} FROM {table} ORDER BY rowid")] for table, columns in TABLES.items()}
    out["triggers_7p"] = sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
        "AND name NOT LIKE 'trg_precedent_%'"))
    conn.close()
    return out


def normalise(payload) -> str:
    """Random ids -> ordinals in order of first appearance; timestamps -> T."""
    text = TIMESTAMP.sub("T", json.dumps(payload, sort_keys=True, ensure_ascii=False))
    seen: dict = {}

    def ordinal(match):
        return seen.setdefault(match.group(0), f"{match.group(1)}#{len(seen)}")
    return ID.sub(ordinal, text)


if __name__ == "__main__":
    db = Path(sys.argv[1])
    stdout, sys.stdout = sys.stdout, open(os.devnull, "w")
    try:
        log = scenario(db)
    finally:
        sys.stdout = stdout
    print(json.dumps({"single_transaction": hasattr(Storage, "record_classification"),
                      "result": normalise({"log": log, "rows": dump(db)})}))
