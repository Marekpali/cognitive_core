"""T2: the STEP 7P adapter input reproduces Probe 7P's corrected result.

The committed test uses synthetic archetypes. The evidence test replays the
real Probe 7P report (production data, not in the repository) when
PROBE7P_REPORT points to it:

    PROBE7P_REPORT=.../cognitive_core_probe7p/ha_input_probe.json pytest tests/test_probe_parity.py
"""

import asyncio
import importlib.util
import json
import os
from pathlib import Path

import pytest

from src.observation_input import build_inputs
from tests.ha_payloads import full_home
from tests.test_sweep import _classifiers

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "ha_input_probe", REPO / "scripts" / "ha_input_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

PROBE7P_SHA256 = "40a87cee273de6ada8be728b0f6b15fa2c81951098fe117b23480b921e0fe8b2"


def _matched(snapshot):
    classifiers = _classifiers()

    async def run():
        return sorted([name for name, c in classifiers.items()
                       if await c.classify(json.loads(json.dumps(snapshot)))])
    return asyncio.run(run())


def test_new_input_equals_probe_hypothesis_on_archetypes():
    devices, entities, states = full_home()
    views = {i.device_id: (i.snapshot or {}).get("entities", [])
             for i in build_inputs(devices, entities, states)}
    report = asyncio.run(probe.build_report(
        devices, entities, states, views, probe.load_classifiers(str(REPO))))
    for row in report["devices"]:
        if row["entities"]:
            assert row["matched_current"] == row["matched_hypothetical"], row["id"]
    assert report["summary"]["devices_changed_by_hypothesis"] == 0


def payloads_from_probe_report(report: dict) -> tuple:
    """Rebuild registry + states from the report. The report records, per
    entity, entity_id/platform/state device_class and the adapter's entity
    names; every entity had a state entry in the probe's hypothesis."""
    devices, entities, states = [], [], []
    for row in report["devices"]:
        devices.append({k: row[k] for k in ("id", "name", "manufacturer", "model")})
        names = {e["entity_id"]: e["name"] for e in row["adapter_input_entities"]}
        for e in row["entities"]:
            entities.append({"entity_id": e["entity_id"], "device_id": row["id"],
                             "platform": e["platform"], "name": names.get(e["entity_id"]),
                             "original_name": None, "disabled_by": None})
            attrs = {"device_class": e["state_device_class"]} if e["state_device_class"] else {}
            states.append({"entity_id": e["entity_id"], "state": "x", "attributes": attrs})
    return devices, entities, states


@pytest.mark.skipif(not os.getenv("PROBE7P_REPORT"), reason="PROBE7P_REPORT not set")
def test_replay_of_production_probe_report():
    import hashlib
    path = Path(os.environ["PROBE7P_REPORT"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == PROBE7P_SHA256
    report = json.loads(path.read_text(encoding="utf-8"))
    rows = {r["id"]: r for r in report["devices"]}

    inputs = build_inputs(*payloads_from_probe_report(report))
    with_entities = [i for i in inputs if not i.skipped]
    matched = {i.device_id: _matched(i.snapshot) for i in with_entities}

    for device_id, names in matched.items():
        assert names == rows[device_id]["matched_hypothetical"], device_id
    assert len(inputs) == 160
    assert len(with_entities) == 148
    assert sum(bool(m) for m in matched.values()) == 18
    assert sum(not m for m in matched.values()) == 130
    assert sum(len(m) for m in matched.values()) == 20
    assert sum(m != rows[d]["matched_current"] for d, m in matched.items()) == 4
