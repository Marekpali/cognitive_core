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
# S4 compares sweeps far enough apart for real state traffic in between
# ("at least one daily sweep"), not e.g. a reconnect seconds earlier.
MIN_S4_GAP_HOURS = 20.0
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


def s3_probe_parity(sweeps) -> tuple:
    """Latest sweep that attempted every device (baseline-free) vs Probe 7P."""
    full = [s for s in sweeps if s["unchanged"] == 0]
    if not full:
        return None, "no sweep that attempted every device"
    devices = json.loads(full[-1]["devices_json"]).values()
    matched = [e["matched_classifiers"] for e in devices if e["matched_classifiers"]]
    got = {"classified": len(matched), "matches": sum(len(m) for m in matched),
           "motion": sum("motion_sensor" in m for m in matched)}
    detail = f"sweep {full[-1]['id']}: {got} vs probe {PROBE}"
    return (True, detail) if got == PROBE else (None, detail + " -> explain differences")


def _hours_between(earlier: str, later: str) -> float:
    parse = lambda ts: datetime.fromisoformat(ts.replace("Z", "+00:00"))  # noqa: E731
    return (parse(later) - parse(earlier)).total_seconds() / 3600


def s4_gate_stability(sweeps) -> tuple:
    by_id = {s["id"]: s for s in sweeps}
    daily = [s for s in sweeps if "daily" in s["source"] and s["baseline_sweep_id"] in by_id
             and _hours_between(by_id[s["baseline_sweep_id"]]["finished_at"],
                                s["finished_at"]) >= MIN_S4_GAP_HOURS]
    if not daily:
        return False, (f"no daily shadow sweep with a stored baseline at least "
                       f"{MIN_S4_GAP_HOURS:g} h older yet")
    s = daily[-1]
    entries = json.loads(s["devices_json"])
    baseline = json.loads(by_id[s["baseline_sweep_id"]]["devices_json"])
    gated = {d: e for d, e in entries.items() if e["outcome"] != "skipped"}
    stable = [d for d, e in gated.items() if e["outcome"] == "unchanged"
              and e["fingerprint"] == e["baseline_fingerprint"]
              == baseline.get(d, {}).get("fingerprint")]
    moved = sorted(set(gated) - set(stable))
    return (bool(gated) and not moved, f"sweep {s['id']} vs {s['baseline_sweep_id']}: "
            f"{len(stable)}/{len(gated)} unchanged; not unchanged: {moved or 'none'}")


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
