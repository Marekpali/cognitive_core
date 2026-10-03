"""STEP 7a: writes precedent annotations inside the caller's transaction.

Both entry points run in a SAVEPOINT of a transaction that belongs to
something more important - the classifier input and observations, or a
human decision. They never raise and never roll back anything but their
own savepoint: precedent memory is information without authority, and
neither it nor its audit may stop the pipeline or a human decision.

    annotate_in_transaction()   normal path: an observation just written
    bootstrap_in_transaction()  a case that was pending before the layer
                                started, annotated immediately before its
                                first human decision is recorded
"""

from datetime import datetime, timezone
from typing import Callable, Optional

from src import precedent_store
from src.learning.precedent import (
    CLASS_PATTERN, DEVICE_PRECEDENT, POLICY_VERSION,
    evaluate_class_pattern, evaluate_device_precedent,
)

SOURCE_OBSERVATION = "observation"
SOURCE_BOOTSTRAP = "bootstrap_pending"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _observation(cursor, observation_id: str) -> dict:
    row = cursor.execute(
        "SELECT id, device_id, classifier_name, hypothesis_category, created_at "
        "FROM classification_observations WHERE id = ?", (observation_id,)).fetchone()
    if row is None:
        raise LookupError(f"observation {observation_id} not found")
    return dict(zip(("id", "device_id", "classifier_name", "hypothesis_category",
                     "created_at"), row))


def _in_savepoint(cursor, name: str, work: Callable[[], None]) -> Optional[Exception]:
    """Run `work` in a savepoint. On failure roll back only that savepoint
    and return the exception."""
    cursor.execute(f"SAVEPOINT {name}")
    try:
        work()
    except Exception as exc:  # noqa: BLE001 - nothing here may escape
        cursor.execute(f"ROLLBACK TO {name}")
        cursor.execute(f"RELEASE {name}")
        return exc
    cursor.execute(f"RELEASE {name}")
    return None


def _guarded(cursor, source: str, observation_id: str, review_case_id: Optional[str],
             work: Callable[[], None]) -> None:
    """Annotation in its savepoint; on failure an audit row in another one.
    A failing audit is logged and otherwise ignored.

    The one thing that is not swallowed: an error after which SQLite has
    dropped the surrounding transaction itself (disk full, I/O error). The
    caller's earlier writes are gone then, so it must fail loudly instead
    of committing what is left."""
    try:
        error = _in_savepoint(cursor, "precedent_annotation", work)
        if error is None:
            return
        print(f"[PRECEDENT] WARNING: annotation failed for {observation_id} "
              f"({source}): {error!r}")
        audit_error = _in_savepoint(
            cursor, "precedent_audit",
            lambda: precedent_store.insert_audit_failure(
                cursor, source, observation_id, review_case_id, error, _now(), POLICY_VERSION))
        if audit_error is not None:
            print(f"[PRECEDENT] WARNING: audit of the failed annotation could not be "
                  f"written for {observation_id}: {audit_error!r}")
    except Exception as exc:  # noqa: BLE001 - savepoint handling itself failed
        print(f"[PRECEDENT] WARNING: precedent layer error for {observation_id}: {exc!r}")
        if not cursor.connection.in_transaction:
            raise


def annotate_in_transaction(cursor, observation_id: str) -> None:
    """Normal path. Device precedent and class pattern for one observation
    that the surrounding transaction has just written."""

    def work() -> None:
        observation = _observation(cursor, observation_id)
        created_at = _now()
        decisions = precedent_store.labelled_decisions(
            cursor, observation["classifier_name"], observation["hypothesis_category"],
            before=created_at)
        own = [d for d in decisions if d.device_id == observation["device_id"]]
        others = [d for d in decisions if d.device_id != observation["device_id"]]
        candidates = (
            (DEVICE_PRECEDENT, evaluate_device_precedent(own[0] if own else None)),
            (CLASS_PATTERN, evaluate_class_pattern(others)),
        )
        for memory_type, annotation in candidates:
            if annotation is None or precedent_store.annotation_exists(
                    cursor, observation_id, memory_type):
                continue
            precedent_store.insert_annotation(
                cursor, observation, annotation, SOURCE_OBSERVATION, created_at, POLICY_VERSION)

    _guarded(cursor, SOURCE_OBSERVATION, observation_id, None, work)


def bootstrap_in_transaction(cursor, case_id: str, observation_id: str,
                             layer_started_at: str) -> Optional[str]:
    """Bootstrap exception. Called by the canonical resolver after its
    checks passed and before the decision is written.

    Returns the timestamp the decision must be strictly later than (the
    annotation's created_at), or None if the case is not eligible.
    """
    written_at: list = []

    def work() -> None:
        observation = _observation(cursor, observation_id)
        if (precedent_store._instant(observation["created_at"])
                >= precedent_store._instant(layer_started_at)):
            return                      # normal cohort: annotated when observed
        if precedent_store.annotation_exists(cursor, observation_id, CLASS_PATTERN):
            return
        created_at = _now()
        written_at.append(created_at)
        annotation = evaluate_class_pattern(precedent_store.labelled_decisions(
            cursor, observation["classifier_name"], observation["hypothesis_category"],
            before=created_at, exclude_device_id=observation["device_id"]))
        if annotation is not None:      # no evidence is not a failure
            precedent_store.insert_annotation(
                cursor, observation, annotation, SOURCE_BOOTSTRAP, created_at, POLICY_VERSION)

    _guarded(cursor, SOURCE_BOOTSTRAP, observation_id, case_id, work)
    return written_at[0] if written_at else None
