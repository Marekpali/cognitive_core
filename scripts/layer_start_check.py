"""
STEP 7a enable check (precedent_mode off -> shadow). Complements d3_verify.py,
which stays the cumulative verifier; this one checks the activation itself.

Run INSIDE the container with the pre-enable backup at /tmp/pre_7a.db:

    docker exec app_local_cognitive_core python3 /share/7a/layer_start_check.py

  OPTION   /data/options.json: precedent_mode shadow, observation_mode active.
  CUTOFF   exactly one precedent_audit row, event layer_started, source NULL,
           and it is not in the snapshot.
  ORDER    of the sweeps written since the snapshot: every `startup` sweep
           started after the cut-off, and the first sweep after the cut-off
           is a `startup` sweep. Sweeps of the previous process (registry
           events before the restart) may precede the cut-off; a startup
           sweep may not.
  QUIET    no annotation_failed, no annotation, no evidence row; review
           cases by status as in the snapshot; no observation, input or
           review case written since the snapshot (a natural registry
           change would show here - judge it, do not ignore it).

Never writes to the live database (mode=ro). Prints ids, counts and
timestamps only. Exit 0 = PASS.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

QUIET_TABLES = ("classification_inputs", "classification_observations", "review_cases")
PRECEDENT_ROWS = (("precedent_annotations", ""), ("precedent_annotation_evidence", ""),
                  ("precedent_audit", "WHERE event = 'annotation_failed'"))


def _instant(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _count(conn, table: str, where: str = "") -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]


def _cases(conn) -> list:
    return conn.execute(
        "SELECT status, COUNT(*) FROM review_cases GROUP BY status ORDER BY status").fetchall()


def check_option(options: Path) -> list:
    if not options.exists():
        return [f"{options} not found"]
    values = json.loads(options.read_text(encoding="utf-8"))
    print(f"  options: {json.dumps(values, sort_keys=True)}")
    expected = {"precedent_mode": "shadow", "observation_mode": "active"}
    return [f"{key} is {values.get(key)!r}, expected {value!r}"
            for key, value in expected.items() if values.get(key) != value]


def check_cutoff(pre, live) -> tuple:
    """Returns (failures, cut-off timestamp or None)."""
    rows = live.execute("SELECT id, event, source, created_at FROM precedent_audit "
                        "ORDER BY rowid").fetchall()
    started = [r for r in rows if r[1] == "layer_started"]
    print(f"  precedent_audit rows: {len(rows)}; layer_started: {len(started)}")
    if len(started) != 1:
        return [f"{len(started)} layer_started rows, expected exactly 1"], None
    row_id, _, source, created_at = started[0]
    print(f"  layer_started {row_id} source={source!r} created_at={created_at}")
    failures = [] if source is None else [f"layer_started has source {source!r}"]
    if _count(pre, "sqlite_master", "WHERE name = 'precedent_audit'") and _count(
            pre, "precedent_audit", "WHERE event = 'layer_started'"):
        failures.append("the snapshot already contains a layer_started row")
    return failures, created_at


def check_order(pre, live, cut_off: str) -> list:
    known = {r[0] for r in pre.execute("SELECT id FROM classification_sweeps")}
    new = [r for r in live.execute(
        "SELECT id, mode, source, started_at, unchanged, classified, no_match, error "
        "FROM classification_sweeps ORDER BY rowid") if r[0] not in known]
    failures = []
    for sweep_id, mode, source, started_at, *counts in new:
        after = _instant(started_at) > _instant(cut_off)
        print(f"  sweep {sweep_id} {mode} {source} started_at={started_at} "
              f"{'AFTER' if after else 'BEFORE'} the cut-off; "
              f"unchanged/classified/no_match/error = {'/'.join(map(str, counts))}")
        if source == "startup" and not after:
            failures.append(f"startup sweep {sweep_id} started before the cut-off")
    later = [r for r in new if _instant(r[3]) > _instant(cut_off)]
    if not later:
        failures.append("no sweep after the cut-off yet")
    elif later[0][2] != "startup":
        failures.append(f"first sweep after the cut-off is {later[0][2]}, not startup")
    else:
        gap = (_instant(later[0][3]) - _instant(cut_off)).total_seconds()
        print(f"  cut-off precedes the first startup sweep {later[0][0]} by {gap:.3f} s")
    return failures


def check_quiet(pre, live) -> list:
    failures = []
    for table, where in PRECEDENT_ROWS:
        rows = _count(live, table, where)
        label = "annotation_failed" if where else table
        print(f"  {label}: {rows} (must be 0)")
        if rows:
            failures.append(f"{rows} {label} row(s)")
    before, now = _cases(pre), _cases(live)
    print(f"  review_cases: snapshot {before}, live {now}")
    if before != now:
        failures.append("review cases by status differ from the snapshot")
    for table in QUIET_TABLES:
        added = _count(live, table) - _count(pre, table)
        print(f"  {table}: +{added} since the snapshot")
        if added:
            failures.append(f"{added} new {table} row(s) since the snapshot")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-db", default="/data/core.db")
    parser.add_argument("--pre-db", default="/tmp/pre_7a.db")
    parser.add_argument("--options", default="/data/options.json")
    args = parser.parse_args()
    print("STEP 7a LAYER START CHECK")
    if not Path(args.pre_db).exists():
        print(f"\nRESULT: FAIL - snapshot {args.pre_db} not found")
        return 1
    live = sqlite3.connect(f"file:{args.live_db}?mode=ro", uri=True)
    pre = sqlite3.connect(f"file:{args.pre_db}?mode=ro", uri=True)
    results = {}
    print("\n[OPTION]")
    results["OPTION"] = check_option(Path(args.options))
    print("\n[CUTOFF]")
    results["CUTOFF"], cut_off = check_cutoff(pre, live)
    print("\n[ORDER]")
    results["ORDER"] = check_order(pre, live, cut_off) if cut_off else ["no single cut-off"]
    print("\n[QUIET]")
    results["QUIET"] = check_quiet(pre, live)
    live.close()
    pre.close()

    print()
    for name, failures in results.items():
        print(f"{name:<7} {'PASS' if not failures else 'FAIL'}")
        for failure in failures:
            print(f"  - {failure}")
    ok = not any(results.values())
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
