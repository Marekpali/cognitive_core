"""
STEP 7P activation verification (shadow -> active). Cumulative: the D0, D1,
D2, PINS and DATA sections of d2_verify.py (which must lie next to this
file) plus the active-mode chain

    classification_sweeps -> classification_inputs
                          -> classification_observations -> review_cases

Run INSIDE the container with the pre-activation backup at /tmp/pre_active.db:

    docker exec app_local_cognitive_core python3 /share/7pa/active_verify.py

  D0..DATA  as in d2_verify.py, against pre_active.db: every snapshot row
            still present and identical; review_cases may only advance
            last_observation_id/updated_at to new evidence.
  ACTIVE    nothing is hard-coded: expected rows are derived from the stored
            active sweep rows. The fingerprint gate is replayed from
            classification_inputs; every evaluated device has exactly one
            input in its sweep; every input has exactly its matched
            observations, each linked by input_id and undecided; every
            observed hypothesis has a review case pointing at its newest
            observation; new cases are pending; cases resolved in the
            snapshot are still resolved with identical decision fields.
  REPEAT    a later active sweep exists and the replayed gate holds for it
            (same fingerprint -> unchanged -> nothing written). PENDING
            until a second active sweep has happened; never forced.

Never writes to the live database (mode=ro); all write probes run on copies
in the work dir. Prints ids, counts and hashes only.
Exit 0 = PASS, 2 = INCOMPLETE (only REPEAT pending), 1 = FAIL.
"""

import argparse
import importlib.util
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

EVALUATED = ("classified", "no_match")
CASE_DECISION_FIELDS = ("status", "decision", "corrected_category", "decided_at")
OBS_DECISION_FIELDS = ("human_decision", "corrected_category", "reviewed_at",
                       "review_reason", "reviewed_by")
COUNT_FIELDS = ("skipped_no_entities", "skipped_missing_metadata", "unchanged",
                "classified", "no_match", "error")
COUNTED_TABLES = ("classification_observations", "review_cases",
                  "classification_inputs", "classification_sweeps", "assets")
PENDING = "PENDING"


def _load_d2_verify():
    path = Path(__file__).with_name("d2_verify.py")
    spec = importlib.util.spec_from_file_location("d2_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dicts(conn, sql: str, params=()) -> list:
    cursor = conn.execute(sql, params)
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _ids(conn, table: str) -> set:
    return {row[0] for row in conn.execute(f"SELECT id FROM {table}")}


def _count_key(entry: dict) -> str:
    if entry["outcome"] == "skipped":
        return f"skipped_{entry['reason']}"
    return entry["outcome"]


def _replay_sweep(sweep: dict, latest: dict, inputs: dict) -> list:
    """Re-check one active sweep against the gate state `latest`
    (device_id -> fingerprint of its latest input), advancing it."""
    failures = []
    entries = json.loads(sweep["devices_json"])
    counted = Counter(_count_key(e) for e in entries.values())
    if (len(entries) != sweep["discovered"]
            or any(counted.get(f, 0) != sweep[f] for f in COUNT_FIELDS)):
        failures.append(f"{sweep['id']}: counts do not match devices_json")
    used = set()
    for device_id, e in sorted(entries.items()):
        where, known = f"{sweep['id']}:{device_id}", latest.get(device_id)
        if e["outcome"] == "skipped":
            continue
        if e["outcome"] == "error":
            failures.append(f"{where}: outcome error ({e.get('reason')})")
        elif e["baseline_fingerprint"] != known:
            failures.append(f"{where}: baseline is not the latest input fingerprint")
        elif e["outcome"] == "unchanged":
            if e["fingerprint"] != known or e.get("input_id"):
                failures.append(f"{where}: unchanged without an equal latest input")
        elif e["fingerprint"] == known:
            failures.append(f"{where}: evaluated although the fingerprint was unchanged")
        else:
            row = inputs.get(e.get("input_id"))
            expected = {"device_id": device_id, "fingerprint": e["fingerprint"],
                        "outcome": e["outcome"], "sweep_id": sweep["id"],
                        "matched_classifiers": e["matched_classifiers"]}
            if row is None:
                failures.append(f"{where}: no classification_inputs row")
            elif {k: row[k] for k in expected} != expected:
                failures.append(f"{where}: input {row['id']} does not match the sweep entry")
            else:
                used.add(row["id"])
                latest[device_id] = e["fingerprint"]
    extra = sorted(i for i, row in inputs.items()
                   if row["sweep_id"] == sweep["id"] and i not in used)
    return failures + [f"{sweep['id']}: input {i} not in devices_json" for i in extra]


def _check_observations(new_obs: list, new_inputs: dict) -> list:
    failures = []
    by_input = {}
    for obs in new_obs:
        row = new_inputs.get(obs["input_id"])
        if row is None:
            failures.append(f"{obs['id']}: new observation without a new input")
        elif row["device_id"] != obs["device_id"]:
            failures.append(f"{obs['id']}: device differs from its input")
        else:
            by_input.setdefault(row["id"], []).append(obs["classifier_name"])
        if obs["review_status"] != "pending" or any(obs[f] is not None
                                                    for f in OBS_DECISION_FIELDS):
            failures.append(f"{obs['id']}: new observation already carries a decision")
    for input_id, row in new_inputs.items():
        if sorted(by_input.get(input_id, [])) != row["matched_classifiers"]:
            failures.append(f"{input_id}: observations do not equal matched_classifiers")
    expected = sum(len(row["matched_classifiers"]) for row in new_inputs.values())
    print(f"  new observations: {len(new_obs)}; expected from new inputs: {expected}")
    return failures


def _check_cases(pre, live, new_obs: list) -> list:
    failures = []
    newest = {}
    for obs in _dicts(live, "SELECT id, device_id, classifier_name, hypothesis_category, "
                            "created_at FROM classification_observations"):
        key = (obs["device_id"], obs["classifier_name"], obs["hypothesis_category"])
        newest[key] = max(newest.get(key, ("", "")), (obs["created_at"], obs["id"]))
    cases = {(c["device_id"], c["classifier_name"], c["hypothesis_category"]): c
             for c in _dicts(live, "SELECT * FROM review_cases")}
    before = {c["id"]: c for c in _dicts(pre, "SELECT * FROM review_cases")}
    observed = {(o["device_id"], o["classifier_name"], o["hypothesis_category"])
                for o in new_obs}
    for key in sorted(observed):
        case = cases.get(key)
        if case is None:
            failures.append(f"no review case for a new observation of {key[0]}/{key[1]}")
        elif case["last_observation_id"] != newest[key][1]:
            failures.append(f"{case['id']}: does not point at its newest observation")
    created = [c for c in cases.values() if c["id"] not in before]
    for case in created:
        key = (case["device_id"], case["classifier_name"], case["hypothesis_category"])
        if key not in observed:
            failures.append(f"{case['id']}: new case without a new observation")
        if (case["status"], case["decision"], case["corrected_category"],
                case["decided_at"]) != ("pending", None, None, None):
            failures.append(f"{case['id']}: new case is not plain pending")
    by_id = {c["id"]: c for c in cases.values()}
    resolved = [c for c in before.values() if c["status"] == "resolved"]
    kept = [c for c in resolved if c["id"] in by_id and all(
        by_id[c["id"]][f] == c[f] for f in CASE_DECISION_FIELDS)]
    failures += [f"{c['id']}: resolved case reopened or decision changed"
                 for c in resolved if c not in kept]
    moved = sum(1 for c in before.values() if c["id"] in by_id
                and by_id[c["id"]]["last_observation_id"] != c["last_observation_id"])
    print(f"  review cases: {len(created)} new (all must be pending), "
          f"{moved} existing advanced to new evidence")
    print(f"  resolved in snapshot: {len(resolved)}; still resolved with identical "
          f"decision fields: {len(kept)}")
    return failures


def check_active(pre, live) -> tuple:
    """Returns (ACTIVE failures, REPEAT failures or PENDING)."""
    for table in COUNTED_TABLES:
        before = pre.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        now = live.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {before} in snapshot, {now} live (+{now - before})")
    failures = []
    if live.execute("SELECT COUNT(*) FROM assets").fetchone()[0] != \
            pre.execute("SELECT COUNT(*) FROM assets").fetchone()[0]:
        failures.append("assets changed (STEP 7P writes no assets)")

    old_sweeps = _ids(pre, "classification_sweeps")
    new_sweeps = [s for s in _dicts(live, "SELECT * FROM classification_sweeps ORDER BY rowid")
                  if s["id"] not in old_sweeps]
    active = [s for s in new_sweeps if s["mode"] == "active"]
    if not active:
        return failures + ["no active sweep since the snapshot"], PENDING
    after_first = new_sweeps[new_sweeps.index(active[0]):]
    failures += [f"{s['id']}: shadow sweep after activation"
                 for s in after_first if s["mode"] != "active"]

    inputs = {}
    for row in _dicts(live, "SELECT id, device_id, fingerprint, outcome, matched_classifiers, "
                            "sweep_id FROM classification_inputs ORDER BY rowid"):
        inputs[row["id"]] = dict(row, matched_classifiers=json.loads(row["matched_classifiers"]))
    old_inputs = _ids(pre, "classification_inputs")
    latest = {row["device_id"]: row["fingerprint"]
              for i, row in inputs.items() if i in old_inputs}
    new_inputs = {i: row for i, row in inputs.items() if i not in old_inputs}
    active_ids = {s["id"] for s in active}
    failures += [f"{i}: input of unknown sweep {row['sweep_id']}"
                 for i, row in new_inputs.items() if row["sweep_id"] not in active_ids]

    per_sweep = []
    for s in active:
        per_sweep.append(_replay_sweep(s, latest, new_inputs))
        written = sum(1 for row in new_inputs.values() if row["sweep_id"] == s["id"])
        print(f"  sweep {s['id']} active {s['source']}: discovered {s['discovered']}, "
              f"evaluated {s['classified'] + s['no_match']} "
              f"(classified {s['classified']}), unchanged {s['unchanged']}, "
              f"error {s['error']}, inputs written {written}")
    failures += per_sweep[0]

    old_obs = _ids(pre, "classification_observations")
    new_obs = [o for o in _dicts(live, "SELECT * FROM classification_observations")
               if o["id"] not in old_obs]
    failures += _check_observations(new_obs, new_inputs)
    failures += _check_cases(pre, live, new_obs)

    if len(active) < 2:
        return failures, PENDING
    quiet = sum(1 for s in active[1:] if s["classified"] + s["no_match"] == 0)
    print(f"  later active sweeps: {len(active) - 1}; with nothing evaluated or written: {quiet}")
    return failures, [f for later in per_sweep[1:] for f in later]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src-root", default="/app")
    parser.add_argument("--live-db", default="/data/core.db")
    parser.add_argument("--pre-db", default="/tmp/pre_active.db")
    parser.add_argument("--work-dir", default="/tmp")
    parser.add_argument("--requirements", default="/share/d2/requirements.txt")
    parser.add_argument("--options", default="/data/options.json")
    args = parser.parse_args()
    d2 = _load_d2_verify()
    src_root, work_dir, pre_p = Path(args.src_root), Path(args.work_dir), Path(args.pre_db)

    print("STEP 7P ACTIVE VERIFY (cumulative D0 + D1 + D2 + active chain)")
    for name in d2.DEPLOYED_FILES:
        print(f"deployed {name:<24} {d2.sha256(src_root / 'src' / name)}")
    print(f"pre_active.db SHA256 {d2.sha256(pre_p)}")
    options = Path(args.options)
    print(f"options {options.read_text(encoding='utf-8').strip() if options.exists() else 'absent'}")

    live = sqlite3.connect(f"file:{args.live_db}?mode=ro", uri=True)
    pre = sqlite3.connect(f"file:{pre_p}?mode=ro", uri=True)
    sections = [
        ("D0", lambda: d2.check_d0(live, work_dir / "active_verify_d0.db", src_root)),
        ("D1", lambda: d2.check_d1(live, work_dir / "active_verify_d1.db")),
        ("D2", lambda: d2.check_d2(live, work_dir / "active_verify_d2.db")),
        ("PINS", lambda: d2.check_pins(Path(args.requirements))),
        ("DATA", lambda: d2.check_data(pre, live)),
    ]
    results = {}
    for name, run in sections:
        print(f"\n[{name}]")
        try:
            results[name] = run()
        except Exception as exc:  # a crashing check is a failed check
            results[name] = [f"check raised {exc!r}"]
    print("\n[ACTIVE]")
    try:
        results["ACTIVE"], results["REPEAT"] = check_active(pre, live)
    except Exception as exc:
        results["ACTIVE"], results["REPEAT"] = [f"check raised {exc!r}"], PENDING
    live.close()
    pre.close()

    print()
    for name, failures in results.items():
        status = PENDING if failures == PENDING else ("PASS" if not failures else "FAIL")
        print(f"{name:<7} {status}")
        for failure in ([] if failures == PENDING else failures):
            print(f"  - {failure}")
    failed = any(f and f != PENDING for f in results.values())
    pending = results["REPEAT"] == PENDING
    print(f"\nRESULT: {'FAIL' if failed else 'INCOMPLETE - second active sweep pending' if pending else 'PASS'}")
    return 1 if failed else 2 if pending else 0


if __name__ == "__main__":
    sys.exit(main())
