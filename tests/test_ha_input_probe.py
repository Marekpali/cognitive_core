"""Tests for scripts/ha_input_probe.py (read-only HA classifier-input probe)."""

import asyncio
import importlib.util
from pathlib import Path

from src.adapters.ha import HAAdapter

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "ha_input_probe", REPO / "scripts" / "ha_input_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

MOTION_DEVICE = {"id": "dev_m", "name": "Motion", "name_by_user": None,
                 "manufacturer": "LUMI", "model": "RTCGQ11LM",
                 "identifiers": [["zha", "00:11"]]}
MOTION_ENTITY = {"entity_id": "binary_sensor.hall_motion", "device_id": "dev_m",
                 "platform": "zha", "name": None, "original_name": "Motion",
                 "device_class": None, "original_device_class": "motion"}


MOTION_STATE = {"entity_id": "binary_sensor.hall_motion", "state": "off",
                "attributes": {"device_class": "motion"}}


def _adapter_view(device, entities, states):
    """Run the REAL HAAdapter._enrich_device with a canned registry."""
    adapter = HAAdapter(token="t")
    replies = {"config/entity_registry/list": entities, "get_states": states}

    async def fake_ws_command(command_type, strict=False):
        return replies[command_type]

    adapter._ws_command = fake_ws_command
    return asyncio.run(adapter._enrich_device(dict(device)))["entities"]


def test_step7p_adapter_passes_entity_domain_and_state_device_class():
    """Probe 7P finding fixed in STEP 7P: domain from entity_id (not the
    platform) and device_class from get_states attributes."""
    view = _adapter_view(MOTION_DEVICE, [MOTION_ENTITY], [MOTION_STATE])
    assert view == [{"entity_id": "binary_sensor.hall_motion",
                     "domain": "binary_sensor", "name": "Motion",
                     "device_class": "motion"}]


def test_report_after_step7p_matches_motion_on_current_input():
    states = [MOTION_STATE]
    view = {"dev_m": _adapter_view(MOTION_DEVICE, [MOTION_ENTITY], states)}
    report = asyncio.run(probe.build_report(
        [MOTION_DEVICE], [MOTION_ENTITY], states,
        view, probe.load_classifiers(str(REPO))))

    row = report["devices"][0]
    assert row["entities"][0] == {
        "entity_id": "binary_sensor.hall_motion",
        "domain_from_entity_id": "binary_sensor",
        "platform": "zha",
        "registry_device_class": None,
        "registry_original_device_class": "motion",
        "state_device_class": "motion",
    }
    assert row["matched_current"] == ["motion_sensor"]
    assert row["matched_hypothetical"] == ["motion_sensor"]
    assert report["summary"]["devices_changed_by_hypothesis"] == 0


def test_effective_device_class_follows_ha_precedence():
    reg = {"device_class": "door", "original_device_class": "opening"}
    assert probe.effective_device_class({"device_class": "window"}, reg) == "window"
    assert probe.effective_device_class({}, reg) == "door"
    assert probe.effective_device_class({}, {"original_device_class": "motion"}) == "motion"
    assert probe.effective_device_class({}, {}) is None


def test_devices_without_entities_are_counted_but_not_classified():
    service = {"id": "dev_s", "name": "Sun", "manufacturer": None, "model": None,
               "identifiers": [["sun", "x"]]}
    report = asyncio.run(probe.build_report(
        [service], [], [], {"dev_s": []}, probe.load_classifiers(str(REPO))))
    assert report["summary"]["devices_discovered"] == 1
    assert report["summary"]["devices_with_entities"] == 0
