"""
Read-only probe: what do the classifiers actually receive from Home Assistant?

Run INSIDE the Cognitive Core container:

    docker exec app_local_cognitive_core python3 /tmp/ha_input_probe.py

Opens its own Home Assistant WebSocket connection (SUPERVISOR_TOKEN), reads
the device registry, entity registry and states, and for every device shows:
  - per entity: entity_id, domain derived from entity_id, platform,
    registry device_class, registry original_device_class, state device_class
  - the entity list the CURRENT adapter passes to classifiers, produced by
    the real HAAdapter._enrich_device() from /app/src (not a re-implementation)
  - classifier results on that current input, and - as a HYPOTHESIS only -
    on an input with the entity-id domain and the effective device class

Never touches /data/core.db (does not import Storage), never subscribes to
events, sends only read commands. Writes a JSON report to --out (default
/tmp/ha_input_probe.json) and prints a short summary.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

DEFAULT_WS_URL = "ws://supervisor/core/websocket"


def entity_domain(entity_id: str) -> str | None:
    return entity_id.split(".", 1)[0] if entity_id and "." in entity_id else None


def effective_device_class(state_attrs: dict, reg: dict) -> str | None:
    """HA order: the entity's own class (visible in state attributes),
    otherwise registry override, otherwise the integration's original."""
    return (state_attrs.get("device_class") or reg.get("device_class")
            or reg.get("original_device_class"))


def hypothetical_entity(reg: dict, state_attrs: dict) -> dict:
    return {
        "name": reg.get("name") or reg.get("original_name"),
        "domain": entity_domain(reg.get("entity_id")),
        "entity_id": reg.get("entity_id"),
        "device_class": effective_device_class(state_attrs, reg),
        "original_device_class": reg.get("original_device_class"),
    }


async def classify_all(classifiers: dict, device: dict) -> dict:
    results = {}
    for name, classifier in classifiers.items():
        result = await classifier.classify(device)
        results[name] = None if result is None else {
            "category": result.get("category"),
            "confidence": result.get("confidence"),
            "reasoning": result.get("reasoning"),
        }
    return results


def _matched(results: dict) -> list:
    return sorted(k for k, v in results.items() if v)


async def build_report(devices, entities, states, adapter_views, classifiers) -> dict:
    """Pure assembly of the report from already-fetched HA data."""
    attrs_by_entity = {s["entity_id"]: s.get("attributes", {}) for s in states}
    entities_by_device = {}
    for reg in entities:
        entities_by_device.setdefault(reg.get("device_id"), []).append(reg)

    rows = []
    for device in devices:
        regs = sorted(entities_by_device.get(device.get("id"), []),
                      key=lambda r: r.get("entity_id") or "")
        base = {k: device.get(k) for k in ("id", "name", "name_by_user",
                                           "manufacturer", "model")}
        adapter_input = dict(base, entities=adapter_views.get(device.get("id"), []))
        hypothetical_input = dict(base, entities=[
            hypothetical_entity(r, attrs_by_entity.get(r.get("entity_id"), {}))
            for r in regs])
        current = await classify_all(classifiers, adapter_input)
        hypothetical = await classify_all(classifiers, hypothetical_input)
        rows.append({
            **base,
            "integrations": sorted({i[0] for i in device.get("identifiers") or []
                                    if isinstance(i, (list, tuple)) and i}),
            "entities": [{
                "entity_id": r.get("entity_id"),
                "domain_from_entity_id": entity_domain(r.get("entity_id")),
                "platform": r.get("platform"),
                "registry_device_class": r.get("device_class"),
                "registry_original_device_class": r.get("original_device_class"),
                "state_device_class": attrs_by_entity.get(
                    r.get("entity_id"), {}).get("device_class"),
            } for r in regs],
            "adapter_input_entities": adapter_input["entities"],
            "classification_current": current,
            "classification_hypothetical": hypothetical,
            "matched_current": _matched(current),
            "matched_hypothetical": _matched(hypothetical),
        })
    return {"summary": summarize(rows, entities), "devices": rows}


def summarize(rows: list, entities: list) -> dict:
    with_entities = [r for r in rows if r["entities"]]
    return {
        "devices_discovered": len(rows),
        "devices_with_entities": len(with_entities),
        "classified_current": sum(bool(r["matched_current"]) for r in with_entities),
        "no_match_current": sum(not r["matched_current"] for r in with_entities),
        "classified_hypothetical": sum(bool(r["matched_hypothetical"]) for r in with_entities),
        "no_match_hypothetical": sum(not r["matched_hypothetical"] for r in with_entities),
        "devices_changed_by_hypothesis": sum(
            r["matched_current"] != r["matched_hypothetical"] for r in with_entities),
        "entity_registry_list_keys": sorted({k for e in entities for k in e}),
    }


async def fetch(ws_url: str, token: str, src_root: str):
    import aiohttp
    sys.path.insert(0, src_root)
    from src.adapters.ha import HAAdapter

    adapter = HAAdapter(token=token, ws_url=ws_url)
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url) as ws:
            adapter.ws = ws
            await adapter._authenticate(ws)
            devices = await adapter._ws_command("config/device_registry/list")
            entities = await adapter._ws_command("config/entity_registry/list")
            states = await adapter._ws_command("get_states")
            views = {}
            for device in devices:
                enriched = await adapter._enrich_device(dict(device))
                views[device.get("id")] = enriched.get("entities", [])
    return devices, entities, states, views


def load_classifiers(src_root: str) -> dict:
    sys.path.insert(0, src_root)
    from src.classifiers.energy import EnergyMeterClassifier
    from src.classifiers.environmental import EnvironmentalSensorClassifier
    from src.classifiers.motion import MotionSensorClassifier
    return {"energy_meter": EnergyMeterClassifier(),
            "environmental_sensor": EnvironmentalSensorClassifier(),
            "motion_sensor": MotionSensorClassifier()}


def print_summary(report: dict) -> None:
    for key, value in report["summary"].items():
        print(f"{key}: {value}")
    print("\ndevice | manufacturer / model | entities | now -> hypothetical")
    for r in report["devices"]:
        if not r["entities"]:
            continue
        now = ",".join(r["matched_current"]) or "no_match"
        hyp = ",".join(r["matched_hypothetical"]) or "no_match"
        flag = "" if now == hyp else "   <-- differs"
        print(f"{r['name_by_user'] or r['name']} | {r['manufacturer']} / "
              f"{r['model']} | {len(r['entities'])} | {now} -> {hyp}{flag}")


async def main_async(args) -> int:
    token = os.getenv("SUPERVISOR_TOKEN") or os.getenv("HA_TOKEN")
    if not token:
        print("ERROR: no SUPERVISOR_TOKEN / HA_TOKEN in the environment")
        return 2
    ws_url = os.getenv("HA_WS_URL") or DEFAULT_WS_URL
    devices, entities, states, views = await fetch(ws_url, token, args.src_root)
    report = await build_report(devices, entities, states, views,
                                load_classifiers(args.src_root))
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    print_summary(report)
    print(f"\nfull report: {args.out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src-root", default="/app")
    parser.add_argument("--out", default="/tmp/ha_input_probe.json")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
