"""
STEP 7a review check: what the human decisions made since a snapshot did to
the precedent layer. Complements d3_verify.py (cumulative) - this one shows
the chain  human decision -> evidence -> annotation of the next decision.

Run INSIDE the container with the pre-review backup at /tmp/pre_review.db:

    docker exec app_local_cognitive_core python3 /share/rv/review_check.py

For every case resolved since the snapshot, in decision order: the
decision, its bootstrap annotation (if any) and the evidence rows behind it.

  LAYER      exactly one layer_started; no annotation_failed since the
             snapshot.
  DECISIONS  every case resolved since the snapshot was pending in it, was
             decided after the cut-off, and its observation carries the
             same decision at the same instant.
  EVIDENCE   every annotation of a newly decided observation was written
             before that decision and after all of its evidence; its
             evidence is exactly the decisions for the same (classifier,
             category) on OTHER devices that existed when the annotation
             was due (the decision for a pre-cut-off observation, the
             observation itself for a newer one) - never the decision being
             recorded; sample_size equals the rows. A decided case without
             an annotation must have had no such earlier decision.
  UNTOUCHED  cases resolved in the snapshot are identical; no new input,
             observation or case since the snapshot.

Never writes to the live database (mode=ro). Prints ids, counts, decisions
and timestamps only - never device names. Exit 0 = PASS.
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

CASE_FIELDS = "id, device_id, classifier_name, hypothesis_category, status, decision, " \
              "corrected_category, decided_at, last_observation_id"
STABLE_TABLES = ("classification_inputs", "classification_observations", "review_cases")


def _instant(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _dicts(conn, sql: str, params=()) -> list:
    cursor = conn.execute(sql, params)
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _label(decision: str, category) -> str:
    return decision + (f":{category}" if category else "")


def check_layer(pre, live) -> tuple:
    started = [r[0] for r in live.execute(
        "SELECT created_at FROM precedent_audit WHERE event = 'layer_started'")]
    failed = live.execute("SELECT COUNT(*) FROM precedent_audit "
                          "WHERE event = 'annotation_failed'").fetchone()[0]
    failed_before = pre.execute("SELECT COUNT(*) FROM precedent_audit "
                                "WHERE event = 'annotation_failed'").fetchone()[0]
    print(f"  layer_started rows: {len(started)}"
          + (f" (cut-off {started[0]})" if len(started) == 1 else ""))
    print(f"  annotation_failed: {failed} ({failed - failed_before} since the snapshot)")
    failures = [] if len(started) == 1 else [f"{len(started)} layer_started rows"]
    if failed != failed_before:
        failures.append(f"{failed - failed_before} annotation_failed row(s) since the snapshot")
    return failures, (started[0] if len(started) == 1 else None)


def _new_decisions(pre, live) -> list:
    before = {c["id"]: c for c in _dicts(pre, f"SELECT {CASE_FIELDS} FROM review_cases")}
    decided = [c for c in _dicts(live, f"SELECT {CASE_FIELDS} FROM review_cases "
                                       "WHERE status = 'resolved'")
               if before.get(c["id"], {}).get("status") != "resolved"]
    for case in decided:
        case["was"] = before.get(case["id"], {}).get("status")
    return sorted(decided, key=lambda c: _instant(c["decided_at"]))


def check_decisions(live, decided: list, cut_off) -> list:
    failures = []
    for n, case in enumerate(decided, 1):
        obs = live.execute("SELECT human_decision, corrected_category, reviewed_at "
                           "FROM classification_observations WHERE id = ?",
                           (case["last_observation_id"],)).fetchone()
        print(f"  #{n} {case['id']} {case['classifier_name']}/{case['hypothesis_category']} "
              f"device {case['device_id']}: {_label(case['decision'], case['corrected_category'])} "
              f"at {case['decided_at']}")
        if case["was"] != "pending":
            failures.append(f"{case['id']}: was {case['was']!r} in the snapshot, not pending")
        if cut_off and _instant(case["decided_at"]) <= _instant(cut_off):
            failures.append(f"{case['id']}: decided before the cut-off")
        if obs is None or tuple(obs) != (case["decision"], case["corrected_category"],
                                         case["decided_at"]):
            failures.append(f"{case['id']}: observation does not carry the same decision")
    return failures


def _expected_evidence(live, case: dict, cut_off) -> set:
    """Labelled observations for the same (classifier, category) on other
    devices, decided before the moment the annotation is due: the decision
    itself for an observation older than the cut-off (bootstrap), the
    observation's own creation for a newer one (annotated when observed)."""
    created_at = live.execute("SELECT created_at FROM classification_observations WHERE id = ?",
                              (case["last_observation_id"],)).fetchone()[0]
    bootstrap = cut_off is None or _instant(created_at) < _instant(cut_off)
    due = _instant(case["decided_at"] if bootstrap else created_at)
    return {r[0] for r in live.execute(
        "SELECT id, reviewed_at FROM classification_observations "
        "WHERE human_decision IS NOT NULL AND classifier_name = ? "
        "AND hypothesis_category = ? AND device_id != ?",
        (case["classifier_name"], case["hypothesis_category"], case["device_id"]))
        if _instant(r[1]) < due}


def check_evidence(live, decided: list, cut_off) -> list:
    failures = []
    for n, case in enumerate(decided, 1):
        expected = _expected_evidence(live, case, cut_off)
        annotations = _dicts(live, "SELECT * FROM precedent_annotations "
                                   "WHERE observation_id = ?", (case["last_observation_id"],))
        if not annotations:
            print(f"  #{n} {case['id']}: no annotation; earlier decisions on other devices: "
                  f"{len(expected)}")
            if expected:
                failures.append(f"{case['id']}: {len(expected)} earlier decision(s) existed "
                                "but no annotation was written")
            continue
        for a in annotations:
            rows = _dicts(live, "SELECT * FROM precedent_annotation_evidence "
                                "WHERE annotation_id = ? ORDER BY decided_at", (a["id"],))
            suggestion = ("ambiguous" if a["result"] == "ambiguous" else
                          _label(a["suggested_outcome"], a["suggested_category"]))
            print(f"  #{n} {case['id']}: {a['id']} {a['memory_type']} {a['annotation_trigger']} "
                  f"-> {suggestion}, evidence {a['evidence_count']}/{a['sample_size']} "
                  f"{a['evidence_maturity']} n={a['sample_size']}, created_at {a['created_at']}")
            for e in rows:
                print(f"       evidence: {e['review_case_id']} obs {e['labelled_observation_id']} "
                      f"device {e['device_id']} "
                      f"{_label(e['human_decision'], e['corrected_category'])} at {e['decided_at']}")
            where = f"{case['id']}/{a['id']}"
            if _instant(a["created_at"]) >= _instant(case["decided_at"]):
                failures.append(f"{where}: annotation not written before the decision")
            if len(rows) != a["sample_size"]:
                failures.append(f"{where}: {len(rows)} evidence rows, sample_size {a['sample_size']}")
            ids = {e["labelled_observation_id"] for e in rows}
            if case["last_observation_id"] in ids:
                failures.append(f"{where}: the decision is in its own evidence")
            if any(_instant(e["decided_at"]) >= _instant(a["created_at"]) for e in rows):
                failures.append(f"{where}: evidence decided after the annotation")
            if a["memory_type"] == "class_pattern" and ids != expected:
                failures.append(f"{where}: evidence is not exactly the earlier decisions "
                                "on other devices")
    return failures


def check_untouched(pre, live) -> list:
    failures = []
    for case in _dicts(pre, f"SELECT {CASE_FIELDS} FROM review_cases WHERE status = 'resolved'"):
        now = _dicts(live, f"SELECT {CASE_FIELDS} FROM review_cases WHERE id = ?", (case["id"],))
        if not now or now[0] != case:
            failures.append(f"{case['id']}: resolved in the snapshot, now different")
    print(f"  review_cases: snapshot "
          f"{pre.execute('SELECT status, COUNT(*) FROM review_cases GROUP BY 1 ORDER BY 1').fetchall()}"
          f", live "
          f"{live.execute('SELECT status, COUNT(*) FROM review_cases GROUP BY 1 ORDER BY 1').fetchall()}")
    for table in STABLE_TABLES:
        added = (live.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 - pre.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        print(f"  {table}: +{added} since the snapshot")
        if added:
            failures.append(f"{added} new {table} row(s) since the snapshot")
    for table in ("precedent_annotations", "precedent_annotation_evidence"):
        print(f"  {table}: {live.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-db", default="/data/core.db")
    parser.add_argument("--pre-db", default="/tmp/pre_review.db")
    args = parser.parse_args()
    print("STEP 7a REVIEW CHECK")
    if not Path(args.pre_db).exists():
        print(f"\nRESULT: FAIL - snapshot {args.pre_db} not found")
        return 1
    live = sqlite3.connect(f"file:{args.live_db}?mode=ro", uri=True)
    pre = sqlite3.connect(f"file:{args.pre_db}?mode=ro", uri=True)
    results = {}
    print("\n[LAYER]")
    results["LAYER"], cut_off = check_layer(pre, live)
    decided = _new_decisions(pre, live)
    print(f"\n[DECISIONS] cases resolved since the snapshot: {len(decided)}")
    results["DECISIONS"] = check_decisions(live, decided, cut_off)
    print("\n[EVIDENCE]")
    results["EVIDENCE"] = check_evidence(live, decided, cut_off)
    print("\n[UNTOUCHED]")
    results["UNTOUCHED"] = check_untouched(pre, live)
    live.close()
    pre.close()

    print()
    for name, failures in results.items():
        print(f"{name:<10} {'PASS' if not failures else 'FAIL'}")
        for failure in failures:
            print(f"  - {failure}")
    ok = not any(results.values())
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
