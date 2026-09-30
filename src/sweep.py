"""STEP 7P: one gated sweep over every device Home Assistant knows.

docs/STEP7_OBSERVATION_SOURCES.md sections 6 (coverage model), 7 (gate) and
8 (shadow/active). Every source - startup, daily, device/entity registry
events - runs this same sweep.

Per device exactly one outcome:
    skipped (no_entities | missing_metadata) | unchanged | classified | no_match | error

shadow: computes everything, writes only the classification_sweeps row.
active: additionally writes classification_inputs + observations (one
        transaction per device) and refreshes review cases.
"""

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from src import coverage_store
from src.classifiers import CLASSIFIER_SET_VERSION
from src.observation_input import (
    SKIP_MISSING_METADATA, SKIP_NO_ENTITIES, build_inputs, canonical_json, fingerprint,
)

MODES = ("shadow", "active")


@dataclass(frozen=True)
class SweepResult:
    sweep_id: str
    mode: str
    source: str
    counts: dict
    devices: dict
    observations_written: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ObservationSweep:
    """Runs sweeps against one Storage with a fixed classifier set."""

    def __init__(self, storage, classifiers: dict, mode: str,
                 classifier_set_version: str = CLASSIFIER_SET_VERSION):
        if mode not in MODES:
            raise ValueError(f"unknown observation mode {mode!r}")
        self.storage = storage
        self.classifiers = classifiers
        self.mode = mode
        self.version = classifier_set_version

    async def run(self, source: str, devices: list, entities: list,
                  states: list) -> SweepResult:
        started_at = _now()
        sweep_id = f"swp_{uuid4().hex[:12]}"
        conn = self.storage.connect()
        if self.mode == "active":
            baseline_sweep_id, baseline = None, coverage_store.latest_input_fingerprints(conn)
        else:
            baseline_sweep_id, baseline = coverage_store.latest_shadow_baseline(conn)

        counts = dict.fromkeys(coverage_store.SWEEP_COUNT_FIELDS, 0)
        entries: dict = {}
        written = 0
        for device_input in build_inputs(devices, entities, states):
            counts["discovered"] += 1
            counts["excluded_disabled_entities"] += device_input.excluded_disabled
            counts["unavailable_entities"] += device_input.unavailable
            entry, observations = await self._process(
                device_input, baseline.get(device_input.device_id), sweep_id)
            written += observations
            counts[_count_key(entry)] += 1
            entries[device_input.device_id] = entry

        # Written last. If this insert fails after active-mode writes, the
        # committed inputs keep a sweep_id with no sweep row (detectable
        # orphan); the adapter retries the sweep, which then reports those
        # devices as unchanged.
        coverage_store.insert_sweep(conn, {
            "id": sweep_id, "mode": self.mode, "source": source,
            "classifier_set_version": self.version,
            "baseline_sweep_id": baseline_sweep_id,
            "started_at": started_at, "finished_at": _now(),
            **counts, "devices": entries,
        })
        print(f"[SWEEP] {sweep_id} mode={self.mode} source={source} "
              + " ".join(f"{k}={v}" for k, v in counts.items()))
        return SweepResult(sweep_id, self.mode, source, counts, entries, written)

    async def _process(self, device_input, baseline_fp, sweep_id) -> tuple:
        """Returns (devices_json entry, number of observations written)."""
        entry = {"outcome": None, "reason": None, "fingerprint": None,
                 "baseline_fingerprint": baseline_fp, "matched_classifiers": []}
        if device_input.skipped:
            return dict(entry, outcome="skipped", reason=device_input.skip_reason), 0

        fp = fingerprint(device_input.snapshot, self.version)
        entry["fingerprint"] = fp
        if fp == baseline_fp:
            return dict(entry, outcome="unchanged"), 0

        try:
            results = {}
            for name, classifier in self.classifiers.items():
                # Deep copy: a classifier must not be able to alter the
                # snapshot that is fingerprinted and stored.
                results[name] = await classifier.classify(copy.deepcopy(device_input.snapshot))
        except Exception as exc:
            print(f"[SWEEP] classifier error on {device_input.device_id}: {exc!r}")
            return dict(entry, outcome="error", reason=type(exc).__name__), 0

        matched = sorted(name for name, result in results.items() if result)
        entry = dict(entry, outcome="classified" if matched else "no_match",
                     matched_classifiers=matched)
        if self.mode == "shadow":
            return entry, 0

        try:
            written = self._persist(device_input, fp, entry, results, sweep_id)
        except Exception as exc:
            print(f"[SWEEP] write failed for {device_input.device_id}: {exc!r}")
            return dict(entry, outcome="error", reason=type(exc).__name__,
                        matched_classifiers=[]), 0
        return dict(entry, input_id=written[0]), written[1]

    def _persist(self, device_input, fp, entry, results, sweep_id) -> tuple:
        """Active mode: input + observations atomically, then review cases."""
        snapshot = device_input.snapshot
        now = _now()
        input_id = f"inp_{uuid4().hex[:12]}"
        group_id = f"obsg_{uuid4().hex[:12]}"
        observations = [{
            "observation_group_id": group_id,
            "device_id": device_input.device_id,
            "classifier_name": name,
            "hypothesis_category": results[name].get("category") or "unknown",
            "hypothesis_confidence": results[name].get("confidence", 0.0),
            "hypothesis_reasoning": results[name].get("reasoning", ""),
            "device_name": snapshot.get("name") or "",
            "device_model": snapshot.get("model") or "",
            "device_manufacturer": snapshot.get("manufacturer") or "",
            "device_source_adapter": "ha",
            "device_entity_count": len(snapshot["entities"]),
        } for name in entry["matched_classifiers"]]

        observation_ids = self.storage.record_classification_input({
            "id": input_id, "device_id": device_input.device_id, "fingerprint": fp,
            "classifier_set_version": self.version,
            "snapshot_json": canonical_json(snapshot),
            "outcome": entry["outcome"],
            "matched_classifiers": entry["matched_classifiers"],
            "sweep_id": sweep_id, "created_at": now,
        }, observations)

        # Review cases are derived state: the input and observations are
        # already committed, so a failure here must not turn the outcome
        # into an error. Storage.backfill_review_cases() reconciles review
        # cases from observation history on the next start.
        try:
            for obs, observation_id in zip(observations, observation_ids):
                self.storage.upsert_review_case(
                    device_id=obs["device_id"], classifier_name=obs["classifier_name"],
                    hypothesis_category=obs["hypothesis_category"],
                    observation_id=observation_id)
        except Exception as exc:
            print(f"[SWEEP] WARNING: review case refresh failed for "
                  f"{device_input.device_id}: {exc!r} (reconciled on next start)")
        return input_id, len(observation_ids)


def _count_key(entry: dict) -> str:
    if entry["outcome"] == "skipped":
        return {SKIP_NO_ENTITIES: "skipped_no_entities",
                SKIP_MISSING_METADATA: "skipped_missing_metadata"}[entry["reason"]]
    return entry["outcome"]
