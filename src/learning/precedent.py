"""STEP 7a: precedent annotations - pure logic, no I/O.

docs/STEP7_ARCHITECTURE.md. An annotation records what earlier human
decisions suggest about a classifier hypothesis. It is information without
authority: nothing in the system may act on it.

Two memories, never combined:
    device precedent - the human decision already made for this exact
                       (device, classifier, category): recall.
    class pattern    - what humans decided for this (classifier, category)
                       on OTHER devices: prediction.
"""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Optional, Sequence

POLICY_VERSION = "7a.1"

DEVICE_PRECEDENT = "device_precedent"
CLASS_PATTERN = "class_pattern"
MEMORY_TYPES = (DEVICE_PRECEDENT, CLASS_PATTERN)

SUGGESTION = "suggestion"
AMBIGUOUS = "ambiguous"

# Descriptive only: a function of the amount of evidence and nothing else.
# No branch, ranking or decision may depend on the label.
EMERGING_FROM = 3
ESTABLISHED_FROM = 10


@dataclass(frozen=True)
class Decision:
    """One human decision, as copied into the evidence snapshot."""
    labelled_observation_id: str
    device_id: str
    human_decision: str
    corrected_category: Optional[str]
    decided_at: str
    review_case_id: Optional[str] = None


@dataclass(frozen=True)
class Annotation:
    memory_type: str
    result: str
    suggested_outcome: Optional[str]
    suggested_category: Optional[str]
    evidence_count: int
    sample_size: int
    correction_count: int
    outcome_distribution: dict
    evidence_maturity: str
    evidence_as_of: str
    evidence_digest: str
    evidence: tuple


def describe_evidence_maturity(sample_size: int) -> str:
    if sample_size < EMERGING_FROM:
        return "insufficient"
    if sample_size < ESTABLISHED_FROM:
        return "emerging"
    return "established"


def outcome_bucket(decision: Decision) -> str:
    if decision.human_decision == "corrected":
        return f"corrected:{decision.corrected_category}"
    return decision.human_decision


def evidence_digest(decisions: Sequence[Decision], policy_version: str = POLICY_VERSION) -> str:
    """SHA256 over the canonical evidence rows plus the policy version."""
    rows = sorted(
        ([d.labelled_observation_id, d.device_id, d.human_decision,
          d.corrected_category, d.decided_at, d.review_case_id] for d in decisions),
        key=lambda row: row[0])
    canonical = json.dumps({"policy_version": policy_version, "evidence": rows},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _annotation(memory_type: str, decisions: Sequence[Decision],
                policy_version: str) -> Annotation:
    distribution = Counter(outcome_bucket(d) for d in decisions)
    ranked = distribution.most_common()
    tie = len(ranked) > 1 and ranked[0][1] == ranked[1][1]
    outcome = category = None
    supporting = 0
    if not tie:
        bucket, supporting = ranked[0]
        outcome, _, category = bucket.partition(":")
    return Annotation(
        memory_type=memory_type,
        result=AMBIGUOUS if tie else SUGGESTION,
        suggested_outcome=outcome,
        suggested_category=category or None,
        evidence_count=supporting,
        sample_size=len(decisions),
        correction_count=sum(d.human_decision == "corrected" for d in decisions),
        outcome_distribution=dict(sorted(distribution.items())),
        evidence_maturity=describe_evidence_maturity(len(decisions)),
        evidence_as_of=max(d.decided_at for d in decisions),
        evidence_digest=evidence_digest(decisions, policy_version),
        evidence=tuple(decisions),
    )


def evaluate_device_precedent(decision: Optional[Decision],
                              policy_version: str = POLICY_VERSION) -> Optional[Annotation]:
    """The one earlier human decision for this exact logical key, if any."""
    if decision is None:
        return None
    return _annotation(DEVICE_PRECEDENT, [decision], policy_version)


def evaluate_class_pattern(decisions_on_other_devices: Sequence[Decision],
                           policy_version: str = POLICY_VERSION) -> Optional[Annotation]:
    """Modal outcome of independent decisions on other devices. A tie for
    the top bucket is recorded as ambiguous - never broken arbitrarily."""
    if not decisions_on_other_devices:
        return None
    return _annotation(CLASS_PATTERN, list(decisions_on_other_devices), policy_version)
