"""STEP 7P: classifier input built from Home Assistant registry data.

Pure functions - no I/O. See docs/STEP7_OBSERVATION_SOURCES.md, sections 5
(classifier input) and 7 (canonical snapshot and fingerprint gate).

The canonical snapshot contains exactly the stable fields the classifiers
read, and is at the same time the device dict passed to the classifiers:

    device:   id, name, manufacturer, model
    entities: sorted by entity_id; each {entity_id, domain, name, device_class}

It never contains an entity's state value, timestamps, or any state
attribute other than device_class - so sensor readings cannot change the
fingerprint.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

SKIP_NO_ENTITIES = "no_entities"
SKIP_MISSING_METADATA = "missing_metadata"
UNAVAILABLE_STATES = frozenset({"unavailable", "unknown"})


@dataclass(frozen=True)
class DeviceInput:
    """Classifier input for one device, or the reason it cannot be built."""

    device_id: str
    snapshot: Optional[dict]
    skip_reason: Optional[str]
    excluded_disabled: int
    unavailable: int

    @property
    def skipped(self) -> bool:
        return self.skip_reason is not None


def entity_domain(entity_id: Optional[str]) -> Optional[str]:
    """`binary_sensor.hall` -> `binary_sensor` (never the integration)."""
    if not entity_id or "." not in entity_id:
        return None
    return entity_id.split(".", 1)[0]


def build_device_input(device: dict, registry_entities: list,
                       states_by_id: dict) -> DeviceInput:
    """Build the canonical input for one device (ADR section 5).

    registry_entities: this device's entries from config/entity_registry/list.
    states_by_id: entity_id -> state object from get_states.

    Disabled entities are excluded (HA keeps no state for them). An enabled
    entity without a state entry means the device class is unknown, so the
    device is skipped as missing_metadata instead of being classified on
    incomplete input.
    """
    device_id = device.get("id")
    enabled = [e for e in registry_entities if e.get("disabled_by") is None]
    excluded = len(registry_entities) - len(enabled)

    missing = [e for e in enabled if e.get("entity_id") not in states_by_id]
    unavailable = sum(
        1 for e in enabled
        if (states_by_id.get(e.get("entity_id")) or {}).get("state") in UNAVAILABLE_STATES
    )

    if not enabled:
        reason = SKIP_NO_ENTITIES
    elif missing:
        reason = SKIP_MISSING_METADATA
    else:
        reason = None

    snapshot = None
    if reason is None:
        snapshot = {
            "id": device_id,
            "name": device.get("name"),
            "manufacturer": device.get("manufacturer"),
            "model": device.get("model"),
            "entities": sorted(
                (_entity_view(e, states_by_id[e["entity_id"]]) for e in enabled),
                key=lambda view: view["entity_id"],
            ),
        }
    return DeviceInput(device_id, snapshot, reason, excluded, unavailable)


def _entity_view(registry_entry: dict, state: dict) -> dict:
    return {
        "entity_id": registry_entry["entity_id"],
        "domain": entity_domain(registry_entry["entity_id"]),
        "name": registry_entry.get("name") or registry_entry.get("original_name"),
        "device_class": (state.get("attributes") or {}).get("device_class"),
    }


def build_inputs(devices: list, entities: list, states: list) -> list:
    """DeviceInput for every device in the registry, in registry order."""
    states_by_id = {s.get("entity_id"): s for s in states if s.get("entity_id")}
    by_device: dict = {}
    for entry in entities:
        by_device.setdefault(entry.get("device_id"), []).append(entry)
    return [build_device_input(d, by_device.get(d.get("id"), []), states_by_id)
            for d in devices]


def canonical_json(value) -> str:
    """Sorted keys, no insignificant whitespace, UTF-8 characters kept."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def fingerprint(snapshot: dict, classifier_set_version: str) -> str:
    """SHA256 over the canonical snapshot plus the classifier set version."""
    payload = canonical_json({"classifier_set_version": classifier_set_version,
                              "input": snapshot})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
