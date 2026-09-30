"""STEP 7P classifier input and fingerprint (ADR 5, 7.1): T1, T3, T4 (input level)."""

import copy

import pytest

from src.classifiers.motion import MotionSensorClassifier
from src.observation_input import (
    SKIP_MISSING_METADATA, SKIP_NO_ENTITIES, build_device_input, build_inputs,
    canonical_json, entity_domain, fingerprint,
)
from tests.ha_payloads import entity, motion_only, smart_socket, state


def _input(builder=motion_only):
    dev, ents, sts = builder()
    return build_device_input(dev, ents, {s["entity_id"]: s for s in sts})


# --- T1: input ---------------------------------------------------------------

def test_domain_comes_from_entity_id_not_platform():
    snap = _input().snapshot
    assert {e["domain"] for e in snap["entities"]} == {"binary_sensor", "button", "sensor"}
    assert "zha" not in canonical_json(snap)


def test_device_class_comes_from_state_attributes():
    snap = _input().snapshot
    by_id = {e["entity_id"]: e for e in snap["entities"]}
    assert by_id["binary_sensor.hall_presence"]["device_class"] == "motion"
    assert by_id["sensor.hall_presence_battery"]["device_class"] == "battery"


def test_snapshot_has_exactly_the_classifier_fields():
    snap = _input().snapshot
    assert set(snap) == {"id", "name", "manufacturer", "model", "entities"}
    for view in snap["entities"]:
        assert set(view) == {"entity_id", "domain", "name", "device_class"}


def test_entities_sorted_by_entity_id():
    ids = [e["entity_id"] for e in _input().snapshot["entities"]]
    assert ids == sorted(ids)


def test_name_uses_registry_name_then_original_name():
    dev, _, sts = motion_only()
    ents = [entity("binary_sensor.hall_presence", "dev_motion", name="Hall",
                   original_name="Occupancy")]
    snap = build_device_input(dev, ents, {s["entity_id"]: s for s in sts}).snapshot
    assert snap["entities"][0]["name"] == "Hall"


@pytest.mark.asyncio
async def test_motion_classifier_matches_the_new_input():
    result = await MotionSensorClassifier().classify(_input().snapshot)
    assert result["category"] == "motion_sensor"


def test_disabled_entities_excluded_and_counted():
    dev, ents, sts = motion_only()
    ents.append(entity("sensor.hall_presence_lqi", "dev_motion", disabled_by="integration"))
    di = build_device_input(dev, ents, {s["entity_id"]: s for s in sts})
    assert not di.skipped
    assert di.excluded_disabled == 1
    assert "sensor.hall_presence_lqi" not in canonical_json(di.snapshot)


def test_entity_without_device_class_is_valid_null():
    dev, ents, sts = motion_only()
    ents.append(entity("sensor.hall_presence_rssi", "dev_motion", original_name="RSSI"))
    sts.append(state("sensor.hall_presence_rssi", "-70"))
    di = build_device_input(dev, ents, {s["entity_id"]: s for s in sts})
    assert not di.skipped
    rssi = [e for e in di.snapshot["entities"] if e["entity_id"].endswith("rssi")][0]
    assert rssi["device_class"] is None


def test_unavailable_entities_are_counted_not_skipped():
    dev, ents, sts = motion_only()
    sts[0]["state"] = "unavailable"
    di = build_device_input(dev, ents, {s["entity_id"]: s for s in sts})
    # the unavailable sensor plus the identify button (state "unknown")
    assert not di.skipped and di.unavailable == 2


# --- T4: missing metadata (input level) ---------------------------------------

def test_enabled_entity_without_state_is_missing_metadata():
    dev, ents, sts = motion_only()
    di = build_device_input(dev, ents, {s["entity_id"]: s for s in sts[1:]})
    assert di.skip_reason == SKIP_MISSING_METADATA
    assert di.snapshot is None


def test_disabled_entity_without_state_is_not_missing_metadata():
    dev, ents, sts = motion_only()
    ents.append(entity("sensor.hall_presence_lqi", "dev_motion", disabled_by="user"))
    di = build_device_input(dev, ents, {s["entity_id"]: s for s in sts})
    assert not di.skipped


def test_device_without_enabled_entities_is_no_entities():
    dev, ents, _ = motion_only()
    disabled = [dict(e, disabled_by="user") for e in ents]
    assert build_device_input(dev, disabled, {}).skip_reason == SKIP_NO_ENTITIES
    assert build_device_input(dev, [], {}).skip_reason == SKIP_NO_ENTITIES


def test_build_inputs_groups_entities_by_device():
    d1, e1, s1 = motion_only()
    d2, e2, s2 = smart_socket()
    inputs = build_inputs([d1, d2], e2 + e1, s1 + s2)
    assert [i.device_id for i in inputs] == ["dev_motion", "dev_socket"]
    assert all(not i.skipped for i in inputs)


def test_entity_domain():
    assert entity_domain("binary_sensor.x") == "binary_sensor"
    assert entity_domain("nodot") is None
    assert entity_domain(None) is None


# --- T3: state-value independence ----------------------------------------------

def _fp(ents, sts, dev=None, version="1"):
    base_dev, _, _ = motion_only()
    di = build_device_input(dev or base_dev, ents, {s["entity_id"]: s for s in sts})
    return fingerprint(di.snapshot, version)


def test_fingerprint_ignores_state_values_timestamps_and_other_attributes():
    _, ents, sts = motion_only()
    before = _fp(ents, sts)
    changed = copy.deepcopy(sts)
    for s in changed:
        s["state"] = "something else"
        s["last_changed"] = s["last_updated"] = "2031-01-01T00:00:00+00:00"
        s["attributes"]["friendly_name"] = "renamed in state"
        s["attributes"]["unit_of_measurement"] = "%"
        s["attributes"]["icon"] = "mdi:x"
    ents_changed = [dict(e, icon="mdi:y", hidden_by="user", platform="other") for e in ents]
    assert _fp(ents_changed, changed) == before


@pytest.mark.parametrize("mutate", [
    lambda d, e, s: s[0]["attributes"].update(device_class="occupancy"),
    lambda d, e, s: e.append(entity("sensor.new", d["id"], original_name="New"))
    or s.append(state("sensor.new", "1")),
    lambda d, e, s: e[1].update(name="Renamed entity"),
    lambda d, e, s: d.update(name="Renamed device"),
    lambda d, e, s: d.update(manufacturer="Other"),
    lambda d, e, s: d.update(model="Other"),
])
def test_fingerprint_changes_with_classifier_inputs(mutate):
    dev, ents, sts = motion_only()
    before = _fp(ents, sts, dev)
    dev2, ents2, sts2 = copy.deepcopy((dev, ents, sts))
    mutate(dev2, ents2, sts2)
    assert _fp(ents2, sts2, dev2) != before


def test_fingerprint_changes_with_classifier_set_version():
    _, ents, sts = motion_only()
    assert _fp(ents, sts, version="1") != _fp(ents, sts, version="2")


def test_fingerprint_independent_of_registry_order():
    dev, ents, sts = motion_only()
    assert _fp(ents, sts, dev) == _fp(list(reversed(ents)), list(reversed(sts)), dev)
