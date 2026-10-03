"""STEP 7a: one canonical resolver. The legacy decision methods delegate to
resolve_review_case() and can never write a decision around the bootstrap."""

import re
import warnings
from pathlib import Path

import pytest

from src.storage import Storage
from tests.precedent_helpers import count, decided, pending, rows, start_layer
from tests.test_storage import _obs

SRC = Path(__file__).resolve().parent.parent / "src"
LEGACY = {
    "approved": lambda s, obs: s.approve_observation(obs, reason="r"),
    "rejected": lambda s, obs: s.reject_observation(obs),
    "corrected": lambda s, obs: s.correct_observation(obs, "occupancy"),
}


def _legacy(storage, decision, observation_id):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return LEGACY[decision](storage, observation_id)


@pytest.fixture
def layered(tmp_path):
    path = tmp_path / "core.db"
    storage = Storage(path)
    decided(storage, "dev0")
    case = pending(storage, "dev1")
    storage.connection.close()
    storage = start_layer(path)
    yield storage, case
    storage.connection.close()


@pytest.mark.parametrize("decision", sorted(LEGACY))
def test_legacy_method_goes_through_the_resolver_and_the_bootstrap(layered, decision):
    storage, (case, obs) = layered
    result = _legacy(storage, decision, obs)
    assert result == "resolved" and result
    (resolved,) = rows(storage, "review_cases", where=f"WHERE id = '{case}'")
    (row,) = rows(storage, "classification_observations", where=f"WHERE id = '{obs}'")
    assert (resolved["status"], resolved["decision"], row["human_decision"]) == (
        "resolved", decision, decision)
    assert resolved["decided_at"] == row["reviewed_at"]
    (annotation,) = rows(storage, "precedent_annotations")
    assert (annotation["observation_id"], annotation["annotation_trigger"]) == (
        obs, "bootstrap_pending")
    assert annotation["created_at"] < resolved["decided_at"]


def test_legacy_method_calls_the_canonical_resolver(layered, monkeypatch):
    storage, (case, obs) = layered
    calls = []
    monkeypatch.setattr(storage, "resolve_review_case",
                        lambda *a, **kw: calls.append((a, kw)) or "stale")
    assert _legacy(storage, "corrected", obs) == "stale"
    assert calls == [((case,), {"expected_observation_id": obs, "decision": "corrected",
                                "reason": None, "corrected_category": "occupancy",
                                "reviewed_by": "human"})]


def test_observation_without_a_case_is_not_reviewable(layered):
    storage, _ = layered
    orphan = _obs(storage, "g_orphan", "dev7", "env", "environmental")
    result = _legacy(storage, "approved", orphan)
    assert result == "not_reviewable" and not result
    (row,) = rows(storage, "classification_observations", where=f"WHERE id = '{orphan}'")
    assert (row["human_decision"], row["review_status"]) == (None, "pending")
    assert count(storage, "precedent_annotations") == 0


def test_superseded_observation_is_not_reviewable(layered):
    """The case has moved on to newer evidence: the old observation can no
    longer be labelled, by any path."""
    storage, (case, old) = layered
    newer = _obs(storage, "g_newer", "dev1", "env", "environmental")
    storage.upsert_review_case("dev1", "env", "environmental", newer)
    assert _legacy(storage, "rejected", old) == "not_reviewable"
    assert rows(storage, "review_cases", where=f"WHERE id = '{case}'")[0]["status"] == "pending"
    assert _legacy(storage, "rejected", newer) == "resolved"


def test_refusals_are_unchanged(layered):
    storage, (_, obs) = layered
    assert _legacy(storage, "approved", "obs_missing") == "not_found"
    assert _legacy(storage, "approved", obs) == "resolved"
    assert _legacy(storage, "rejected", obs) == "already_resolved"
    assert rows(storage, "classification_observations",
                where=f"WHERE id = '{obs}'")[0]["human_decision"] == "approved"


def test_only_the_resolver_writes_a_decision():
    """Static: across src/ exactly one statement labels an observation and
    exactly one resolves a case, both inside resolve_review_case()."""
    sources = {p: p.read_text(encoding="utf-8") for p in SRC.rglob("*.py")}
    label = re.compile(r"human_decision\s*=\s*\?")
    resolve = re.compile(r"SET\s+status\s*=\s*'resolved'")
    hits = {(p.name, kind) for p, text in sources.items()
            for kind, pattern in (("label", label), ("resolve", resolve))
            for _ in pattern.finditer(text)}
    assert hits == {("storage.py", "label"), ("storage.py", "resolve")}
    text = sources[SRC / "storage.py"]
    assert len(label.findall(text)) == len(resolve.findall(text)) == 1
    start = text.index("    def resolve_review_case(")
    end = text.index("\n    def ", start + 1)
    assert start < label.search(text).start() < end
    assert start < resolve.search(text).start() < end
    legacy = text[text.index("    def _record_legacy_decision("):]
    legacy = legacy[:legacy.index("\n    def ", 1)]
    assert "UPDATE" not in legacy and "INSERT" not in legacy and "commit" not in legacy
