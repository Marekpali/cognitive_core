"""STEP 7a pure logic: suggestions, ties, maturity, digest."""

import re
from pathlib import Path

import pytest

from src.learning.precedent import (
    AMBIGUOUS, CLASS_PATTERN, DEVICE_PRECEDENT, POLICY_VERSION, SUGGESTION, Decision,
    describe_evidence_maturity, evaluate_class_pattern, evaluate_device_precedent,
    evidence_digest,
)

SRC = Path(__file__).resolve().parent.parent / "src"


def _d(n, decision, category=None, at=None):
    return Decision(f"obs_{n}", f"dev_{n}", decision, category,
                    at or f"2026-10-0{n}T10:00:00Z", f"case_{n}")


def test_no_evidence_means_no_annotation():
    assert evaluate_class_pattern([]) is None
    assert evaluate_device_precedent(None) is None


def test_device_precedent_recalls_the_one_decision():
    a = evaluate_device_precedent(_d(1, "corrected", "occupancy"))
    assert (a.memory_type, a.result) == (DEVICE_PRECEDENT, SUGGESTION)
    assert (a.suggested_outcome, a.suggested_category) == ("corrected", "occupancy")
    assert (a.evidence_count, a.sample_size, a.correction_count) == (1, 1, 1)


def test_class_pattern_suggests_the_modal_outcome():
    a = evaluate_class_pattern([_d(1, "approved"), _d(2, "approved"),
                                _d(3, "corrected", "occupancy")])
    assert (a.memory_type, a.result) == (CLASS_PATTERN, SUGGESTION)
    assert (a.suggested_outcome, a.suggested_category) == ("approved", None)
    assert (a.evidence_count, a.sample_size, a.correction_count) == (2, 3, 1)
    assert a.outcome_distribution == {"approved": 2, "corrected:occupancy": 1}
    assert a.evidence_as_of == "2026-10-03T10:00:00Z"


def test_corrected_categories_are_separate_buckets():
    a = evaluate_class_pattern([_d(1, "corrected", "occupancy"), _d(2, "corrected", "occupancy"),
                                _d(3, "corrected", "environmental")])
    assert (a.suggested_outcome, a.suggested_category, a.evidence_count) == (
        "corrected", "occupancy", 2)
    assert a.correction_count == 3


def test_rejected_suggestion_names_no_category():
    a = evaluate_class_pattern([_d(1, "rejected"), _d(2, "rejected"), _d(3, "approved")])
    assert (a.suggested_outcome, a.suggested_category) == ("rejected", None)


@pytest.mark.parametrize("decisions", [
    [_d(1, "approved"), _d(2, "rejected")],
    [_d(1, "corrected", "occupancy"), _d(2, "corrected", "occupancy"),
     _d(3, "corrected", "environmental"), _d(4, "corrected", "environmental")],
])
def test_tie_is_ambiguous_never_an_arbitrary_pick(decisions):
    a = evaluate_class_pattern(decisions)
    assert a.result == AMBIGUOUS
    assert (a.suggested_outcome, a.suggested_category, a.evidence_count) == (None, None, 0)
    assert a.sample_size == len(decisions) and sum(a.outcome_distribution.values()) == len(decisions)


@pytest.mark.parametrize("n, label", [(0, "insufficient"), (1, "insufficient"), (2, "insufficient"),
                                      (3, "emerging"), (9, "emerging"),
                                      (10, "established"), (250, "established")])
def test_maturity_is_a_function_of_the_evidence_count_only(n, label):
    assert describe_evidence_maturity(n) == label


def test_maturity_does_not_depend_on_what_the_evidence_says():
    agree = evaluate_class_pattern([_d(n, "approved") for n in range(1, 5)])
    split = evaluate_class_pattern([_d(1, "approved"), _d(2, "rejected"),
                                    _d(3, "corrected", "x"), _d(4, "corrected", "y")])
    assert agree.evidence_maturity == split.evidence_maturity == "emerging"


def test_nothing_outside_the_function_and_the_report_reads_the_label():
    """The label must not drive any branch: apart from where it is
    computed, stored and displayed, no code mentions it."""
    allowed = {"learning/precedent.py", "precedent_store.py", "precedent_report.py"}
    users = {str(p.relative_to(SRC)).replace("\\", "/") for p in SRC.rglob("*.py")
             if re.search(r"evidence_maturity|describe_evidence_maturity",
                          p.read_text(encoding="utf-8"))}
    assert users == allowed
    for name in ("precedent_store.py", "precedent_report.py", "learning/precedent.py"):
        text = (SRC / name).read_text(encoding="utf-8")
        assert not re.search(r"\b(if|elif|while)\b[^\n]*maturity", text), name
        assert not re.search(r"ORDER BY[^\n]*maturity", text), name


def test_digest_is_order_independent_and_sensitive():
    rows = [_d(1, "approved"), _d(2, "corrected", "occupancy")]
    digest = evidence_digest(rows)
    assert digest == evidence_digest(list(reversed(rows))) and len(digest) == 64
    assert digest != evidence_digest(rows, policy_version="other")
    assert digest != evidence_digest([_d(1, "approved"), _d(2, "corrected", "environmental")])
    assert digest != evidence_digest([_d(1, "rejected"), _d(2, "corrected", "occupancy")])
    assert digest != evidence_digest(rows[:1])
    assert evaluate_class_pattern(rows).evidence_digest == digest
    assert POLICY_VERSION == "7a.1"
