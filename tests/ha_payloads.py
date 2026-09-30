"""Synthetic Home Assistant registry payloads for STEP 7P tests.

Shapes follow what Probe 7P recorded on production (entity registry list
without device_class; device class only in get_states attributes). Names
and ids are synthetic.
"""


def device(device_id, name, manufacturer=None, model=None, name_by_user=None):
    return {"id": device_id, "name": name, "name_by_user": name_by_user,
            "manufacturer": manufacturer, "model": model,
            "identifiers": [["zha", device_id]]}


def entity(entity_id, device_id, name=None, original_name=None,
           platform="zha", disabled_by=None):
    """Entity registry list entry - note: no device_class keys (probe 3.3)."""
    return {"entity_id": entity_id, "device_id": device_id, "platform": platform,
            "name": name, "original_name": original_name,
            "disabled_by": disabled_by, "hidden_by": None, "icon": None}


def state(entity_id, value="on", device_class=None, **attributes):
    attrs = dict(attributes)
    if device_class is not None:
        attrs["device_class"] = device_class
    return {"entity_id": entity_id, "state": value, "attributes": attrs,
            "last_changed": "2026-09-30T20:00:00+00:00",
            "last_updated": "2026-09-30T20:00:00+00:00"}


def _with(dev, specs):
    """specs: (entity_id, original_name, device_class, value)"""
    ents = [entity(eid, dev["id"], original_name=nm) for eid, nm, _, _ in specs]
    sts = [state(eid, val, dc) for eid, _, dc, val in specs]
    return dev, ents, sts


def motion_only():
    """ZG-204Z-like presence sensor: probe no_match -> motion_sensor."""
    return _with(device("dev_motion", "HOBEIAN ZG-204Z", "HOBEIAN", "ZG-204Z"), [
        ("binary_sensor.hall_presence", None, "motion", "off"),
        ("button.hall_presence_identify", "Identify", "identify", "unknown"),
        ("sensor.hall_presence_battery", "Battery", "battery", "87"),
    ])


def multisensor():
    """TS0601-like: probe environmental -> environmental + motion."""
    return _with(device("dev_multi", "_TZE200 TS0601", "_TZE200_x", "TS0601"), [
        ("binary_sensor.room_multi", None, "motion", "on"),
        ("sensor.room_multi_temperature", "Temperature", "temperature", "21.5"),
        ("sensor.room_multi_humidity", "Humidity", "humidity", "44"),
        ("sensor.room_multi_illuminance", "Illuminance", "illuminance", "120"),
        ("sensor.room_multi_battery", "Battery", "battery", "90"),
    ])


def smart_socket():
    """Tuya Smart Socket: energy_meter both before and after the fix."""
    return _with(device("dev_socket", "Desk lamp", "Tuya", "Smart Socket"), [
        ("sensor.desk_lamp_power", "Power", "power", "12.5"),
        ("sensor.desk_lamp_total_energy", "Total energy", "energy", "3.2"),
        ("switch.desk_lamp_socket_1", "Socket 1", "outlet", "on"),
    ])


def app_service():
    """HA app/service device: no_match."""
    return _with(device("dev_app", "Some App", "Official apps", "Home Assistant App"), [
        ("update.some_app_update", "Update", "firmware", "off"),
        ("switch.some_app", "Running", None, "on"),
    ])


def no_entities():
    return device("dev_empty", "Sun", None, None), [], []


def home(*builders):
    """Combine archetypes into one (devices, entities, states) snapshot."""
    devices, entities, states = [], [], []
    for build in builders:
        dev, ents, sts = build()
        devices.append(dev)
        entities += ents
        states += sts
    return devices, entities, states


def full_home():
    return home(motion_only, multisensor, smart_socket, app_service, no_entities)
