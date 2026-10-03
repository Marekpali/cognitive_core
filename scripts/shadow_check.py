"""
STEP 7P shadow evaluation: criteria S1-S6 (docs/STEP7_OBSERVATION_SOURCES.md
10.2), computed only from stored rows. Read-only (mode=ro).

    docker exec app_local_cognitive_core python3 /tmp/shadow_check.py

--pre-db is the pre-D2 backup (for S6). Prints ids and counts only.
Exit 0 = all criteria MET; 1 = something is NOT MET or needs a decision
(CHECK). The GO for activation is a human decision recorded in the D2
attestation; this script only supplies the evidence.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime

PROBE = {"classified": 18, "matches": 20, "motion": 4}   # Probe 7P, corrected input
MISSING_METADATA_LIMIT = 0.05
# S4 needs a window long enough for real state traffic (thousands of state
# changes), not e.g. a reconnect seconds after the startup sweep.
MIN_S4_GAP_HOURS = 20.0
EVALUATED = ("classified", "no_match")
GATE_ADVANCING = EVALUATED + ("unchanged",)
OUTCOME_FIELDS = ("skipped_no_entities", "skipped_missing_metadata", "unchanged",
                  "classified", "no_match", "error")
S6_QUERIES = {
    "classification_observations": "SELECT COUNT(*) FROM classification_observations",
    "review_cases": "SELECT COUNT(*) FROM review_cases",
    "resolved review_cases": "SELECT COUNT(*) FROM review_cases WHERE status = 'resolved'",
    "observations with human_decision":
        "SELECT COUNT(*) FROM classification_observations WHERE human_decision IS NOT NULL",
}


def _shadow_sweeps(conn) -> list:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(
        "SELECT * FROM classification_sweeps WHERE mode = 'shadow' ORDER BY rowid")]


def s1_coverage(sweeps) -> tuple:
    broken = [s["id"] for s in sweeps
              if sum(s[f] for f in OUTCOME_FIELDS) != s["discovered"]
              or len(json.loads(s["devices_json"])) != s["discovered"]]
    last = sweeps[-1]
    return (not broken, f"{len(sweeps)} shadow sweep(s); latest discovered="
            f"{last['discovered']}; invariant broken in: {broken or 'none'}")


def s2_errors(sweeps) -> tuple:
    errors = {s["id"]: s["error"] for s in sweeps if s["error"]}
    return not errors, f"errors: {errors or 'none'}"


def _matches(matched_lists) -> dict:
    matched = [m for m in matched_lists if m]
    return {"classified": len(matched), "matches": sum(len(m) for m in matched),
            "motion": sum("motion_sensor" in m for m in matched)}


def _effective(sweeps) -> dict:
    """Current result per device still present: its latest evaluation."""
    latest = {}
    for s in sweeps:
        for device_id, e in json.loads(s["devices_json"]).items():
            if e["outcome"] in EVALUATED:
                latest[device_id] = e["matched_classifiers"]
    return _matches(latest.get(d) for d in json.loads(sweeps[-1]["devices_json"]))


def s3_probe_parity(sweeps) -> tuple:
    """Probe 7P vs the latest sweep that attempted every device, and vs the
    effective result now (after re-evaluations caused by registry changes)."""
    full = [s for s in sweeps if s["unchanged"] == 0]
    if not full:
        return None, "no sweep that attempted every device"
    got = _matches(e["matched_classifiers"]
                   for e in json.loads(full[-1]["devices_json"]).values())
    now = _effective(sweeps)
    detail = f"sweep {full[-1]['id']}: {got}; effective now: {now}; probe {PROBE}"
    return (True, detail) if got == now == PROBE else (None, detail + " -> explain differences")


def _hours_between(earlier: str, later: str) -> float:
    parse = lambda ts: datetime.fromisoformat(ts.replace("Z", "+00:00"))  # noqa: E731
    return (parse(later) - parse(earlier)).total_seconds() / 3600


def _fingerprints(devices: dict) -> dict:
    return {d: e["fingerprint"] for d, e in devices.items() if e["fingerprint"]}


def _gate_inconsistencies(sweeps, devices_of) -> list:
    """Every stored gate decision re-checked: `unchanged` only with an equal
    baseline fingerprint, a re-evaluation only with a different one, and the
    recorded baseline equal to what the baseline sweep stored."""
    bad = []
    for s in sweeps:
        base = devices_of.get(s["baseline_sweep_id"], {})
        for device_id, e in devices_of[s["id"]].items():
            stored = base.get(device_id, {})
            expected = stored.get("fingerprint") if stored.get("outcome") in GATE_ADVANCING else None
            same = e["fingerprint"] is not None and e["fingerprint"] == e["baseline_fingerprint"]
            if (e["baseline_fingerprint"] != expected
                    or (e["outcome"] == "unchanged" and not same)
                    or (e["outcome"] in EVALUATED and same)):
                bad.append(f"{s['id']}:{device_id}")
    return bad


def _stable_window(sweeps, devices_of) -> tuple:
    """Longest run of consecutive sweeps in which every fingerprinted device
    keeps the fingerprint of the run's first sweep and is gated `unchanged`.
    Returns (hours, first id, last id, sweeps in run, devices)."""
    best = (0.0, None, None, 0, 0)
    for i, first in enumerate(sweeps):
        cohort = _fingerprints(devices_of[first["id"]])
        if not cohort or first["error"]:
            continue
        for count, s in enumerate(sweeps[i + 1:], start=2):
            devices = devices_of[s["id"]]
            if s["error"] or _fingerprints(devices) != cohort or any(
                    devices[d]["outcome"] != "unchanged" for d in cohort):
                break
            hours = _hours_between(first["finished_at"], s["finished_at"])
            if hours > best[0]:
                best = (hours, first["id"], s["id"], count, len(cohort))
    return best


def s4_gate_stability(sweeps) -> tuple:
    """A continuous window >= MIN_S4_GAP_HOURS, any sweep source, in which
    every device keeps an identical fingerprint in every sweep. Changes
    outside the window are real registry changes: reported, and every gate
    decision must be consistent with the stored fingerprints."""
    devices_of = {s["id"]: json.loads(s["devices_json"]) for s in sweeps}
    hours, first_id, last_id, count, cohort = _stable_window(sweeps, devices_of)
    changes, seen = {}, {}
    for s in sweeps:
        for device_id, fp in _fingerprints(devices_of[s["id"]]).items():
            if seen.setdefault(device_id, fp) != fp:
                changes[device_id] = changes.get(device_id, 0) + 1
                seen[device_id] = fp
    first, last = devices_of[sweeps[0]["id"]], devices_of[sweeps[-1]["id"]]
    bad = _gate_inconsistencies(sweeps, devices_of)
    detail = (f"longest stable window {hours:.1f} h (need {MIN_S4_GAP_HOURS:g}): "
              f"{first_id} -> {last_id}, {count} sweeps, {cohort} devices identical; "
              f"whole period: fingerprint changed {len(changes)} "
              f"(more than once {sum(n > 1 for n in changes.values())}), "
              f"added {len(set(last) - set(first))}, removed {len(set(first) - set(last))}; "
              f"gate inconsistencies: {bad or 'none'}")
    return hours >= MIN_S4_GAP_HOURS and cohort > 0 and not bad, detail


def s5_missing_metadata(sweeps) -> tuple:
    last = sweeps[-1]
    with_entities = last["discovered"] - last["skipped_no_entities"]
    ids = sorted(d for d, e in json.loads(last["devices_json"]).items()
                 if e.get("reason") == "missing_metadata")
    share = len(ids) / with_entities if with_entities else 0.0
    return (share <= MISSING_METADATA_LIMIT,
            f"{len(ids)}/{with_entities} = {share:.1%} (limit 5%); devices: {ids or 'none'}")


def s6_no_writes(live, pre) -> tuple:
    diffs = []
    for label, sql in S6_QUERIES.items():
        before, now = pre.execute(sql).fetchone()[0], live.execute(sql).fetchone()[0]
        if before != now:
            diffs.append(f"{label} {before}->{now}")
    inputs = live.execute("SELECT COUNT(*) FROM classification_inputs").fetchone()[0]
    if inputs:
        diffs.append(f"classification_inputs 0->{inputs}")
    return not diffs, f"changes since pre-D2: {diffs or 'none'}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-db", default="/data/core.db")
    parser.add_argument("--pre-db", default="/tmp/pre_d2.db")
    args = parser.parse_args()
    live = sqlite3.connect(f"file:{args.live_db}?mode=ro", uri=True)
    pre = sqlite3.connect(f"file:{args.pre_db}?mode=ro", uri=True)

    print("STEP 7P SHADOW CHECK")
    sweeps = _shadow_sweeps(live)
    if not sweeps:
        print("no shadow sweeps recorded\nRESULT: NOT MET")
        return 1
    results = [
        ("S1 coverage", *s1_coverage(sweeps)),
        ("S2 errors", *s2_errors(sweeps)),
        ("S3 probe parity", *s3_probe_parity(sweeps)),
        ("S4 gate stability", *s4_gate_stability(sweeps)),
        ("S5 missing metadata", *s5_missing_metadata(sweeps)),
        ("S6 no writes", *s6_no_writes(live, pre)),
    ]
    for name, ok, detail in results:
        status = "MET" if ok else ("CHECK" if ok is None else "NOT MET")
        print(f"{name:<20} {status:<8} {detail}")
    all_met = all(ok for _, ok, _ in results)
    print(f"\nRESULT: {'ALL MET - ready for GO decision' if all_met else 'NOT READY'}")
    return 0 if all_met else 1


if __name__ == "__main__":
    sys.exit(main())
