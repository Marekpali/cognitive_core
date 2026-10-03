"""STEP 7a: `precedent-report` (docs/STEP7_ARCHITECTURE.md).

Not one accuracy number. Three sections that are never merged:
device precedent (recall, counted, never scored), class pattern - normal
cohort (annotated when observed), class pattern - bootstrap cohort
(annotated at decision time; a different temporal claim).

Ratios are fractions; a percentage is added only from a denominator of 5.
Every maturity label is shown with its n.
"""

import sqlite3
from datetime import datetime

from src.learning.precedent import AMBIGUOUS, CLASS_PATTERN, DEVICE_PRECEDENT

PERCENT_FROM = 5
WRONG = ("corrected", "rejected")
COHORTS = (("observation", "normal cohort (annotated when observed)"),
           ("bootstrap_pending", "bootstrap cohort (annotated at decision time)"))


def ratio(numerator: int, denominator: int) -> str:
    text = f"{numerator}/{denominator}"
    if denominator >= PERCENT_FROM:
        text += f" ({round(100 * numerator / denominator)}%)"
    return text


def _instant(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def match_class(annotation: dict, decision: str, corrected_category) -> str:
    """How a class-pattern suggestion compares with the human decision."""
    if annotation["result"] == AMBIGUOUS:
        return "ambiguous"
    suggested = annotation["suggested_outcome"]
    if suggested == decision and (
            decision != "corrected" or annotation["suggested_category"] == corrected_category):
        return "matched"
    if suggested in WRONG and decision in WRONG:
        return "direction-only"
    return "mismatched"


def _rows(conn, sql: str, params=()) -> list:
    cursor = conn.execute(sql, params)
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _key(row: dict) -> tuple:
    return row["device_id"], row["classifier_name"], row["hypothesis_category"]


def _cohort(annotations: list) -> dict:
    """Metrics of one class-pattern cohort. One annotation counts per
    resolved logical case: the one on the observation the human judged,
    and only if the decision came after the annotation."""
    verified = []
    for a in annotations:
        if a["human_decision"] is None:
            continue
        if _instant(a["reviewed_at"]) <= _instant(a["created_at"]):
            continue
        verified.append(dict(a, match=match_class(
            a, a["human_decision"], a["decided_category"])))
    scored = [v for v in verified if v["match"] != "ambiguous"]
    verified_keys = {_key(v) for v in verified}
    return {
        "generated": len(annotations),
        "cases": len({_key(a) for a in annotations}),
        "ambiguous": sum(a["result"] == AMBIGUOUS for a in annotations),
        "verifiable": len(scored),
        "matched": sum(v["match"] == "matched" for v in scored),
        "direction_only": sum(v["match"] == "direction-only" for v in scored),
        "mismatched": sum(v["match"] == "mismatched" for v in scored),
        "not_yet_verifiable": len({_key(a) for a in annotations} - verified_keys),
        "verified": verified,
    }


def build_report(conn: sqlite3.Connection) -> dict:
    annotations = _rows(conn, """
        SELECT a.*, o.human_decision, o.corrected_category AS decided_category, o.reviewed_at
        FROM precedent_annotations a
        JOIN classification_observations o ON o.id = a.observation_id
        ORDER BY a.created_at, a.id
    """)
    started = conn.execute(
        "SELECT created_at FROM precedent_audit WHERE event = 'layer_started'").fetchone()
    failed = dict(conn.execute(
        "SELECT source, COUNT(*) FROM precedent_audit "
        "WHERE event = 'annotation_failed' GROUP BY source").fetchall())
    device = [a for a in annotations if a["memory_type"] == DEVICE_PRECEDENT]
    return {
        "layer_started_at": started[0] if started else None,
        "annotation_failed": {source: failed.get(source, 0) for source, _ in COHORTS},
        "device_precedent": {"generated": len(device), "cases": len({_key(a) for a in device})},
        "class_pattern": {
            trigger: _cohort([a for a in annotations if a["memory_type"] == CLASS_PATTERN
                              and a["annotation_trigger"] == trigger])
            for trigger, _ in COHORTS
        },
    }


def render(report: dict) -> str:
    lines = ["PRECEDENT REPORT (STEP 7a - shadow: annotations have no effect)",
             f"layer started at: {report['layer_started_at'] or 'never (layer has not run)'}",
             "annotation_failed: " + ", ".join(
                 f"{source} {count}" for source, count in report["annotation_failed"].items()),
             "",
             "DEVICE PRECEDENT - recall, counted, never scored",
             f"  annotations generated:   {report['device_precedent']['generated']}",
             f"  logical cases annotated: {report['device_precedent']['cases']}"]
    for trigger, title in COHORTS:
        c = report["class_pattern"][trigger]
        lines += ["", f"CLASS PATTERN - {title}",
                  f"  annotations generated:   {c['generated']}",
                  f"  logical cases annotated: {c['cases']}",
                  f"  ambiguous (not scored):  {c['ambiguous']}",
                  f"  verifiable:              {c['verifiable']}",
                  f"  matched:                 {ratio(c['matched'], c['verifiable'])}",
                  f"  direction-only:          {ratio(c['direction_only'], c['verifiable'])}",
                  f"  mismatched:              {ratio(c['mismatched'], c['verifiable'])}",
                  f"  not yet verifiable:      {c['not_yet_verifiable']}"]
        for v in c["verified"]:
            suggestion = ("ambiguous" if v["result"] == AMBIGUOUS else
                          v["suggested_outcome"] + (f":{v['suggested_category']}"
                                                    if v["suggested_category"] else ""))
            decision = v["human_decision"] + (f":{v['decided_category']}"
                                              if v["decided_category"] else "")
            lines.append(
                f"    {v['device_id']} {v['classifier_name']}/{v['hypothesis_category']} -> "
                f"{suggestion} (evidence {ratio(v['evidence_count'], v['sample_size'])}, "
                f"corrections {ratio(v['correction_count'], v['sample_size'])}, "
                f"{v['evidence_maturity']} n={v['sample_size']}) -> {decision} -> {v['match']}")
    if report["class_pattern"]["bootstrap_pending"]["generated"]:
        lines += ["", "Note: the bootstrap cohort's evidence cut-off is the decision time, "
                      "not the observation time; it is never merged with the normal cohort."]
    return "\n".join(lines)
