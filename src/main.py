# src/main.py
import asyncio
import sys
from pathlib import Path
from src.core import CognitiveCore
from src.storage import Storage
from src.ha_sensor import update_pending_reviews
async def main_loop(core: CognitiveCore):
    """Main event loop"""
    await core.start()

    # Listen for device signals
    for adapter in core.adapters.values():
        asyncio.create_task(adapter.listen(core.on_device_detected))

    # STEP 4A.2: periodic reconcile of the pending-reviews sensor.
    # Event-driven pushes (core.py's start()/on_device_detected(), and
    # show_review_queue()'s approve/reject/correct handlers below) are
    # the primary update mechanism and fire immediately. This loop is
    # only a safety net - it catches drift from anything that bypasses
    # those paths (a crashed push, a manual DB edit, etc).
    RECONCILE_INTERVAL_SECONDS = 60
    elapsed_since_reconcile = 0
    # STEP 4A.2: holds references to fire-and-forget reconcile tasks
    # so they aren't garbage-collected mid-execution (see core.py's
    # _fire_and_forget for the same pattern and rationale).
    background_tasks: set = set()

    # Keep running
    while True:
        await asyncio.sleep(1)
        elapsed_since_reconcile += 1
        if elapsed_since_reconcile >= RECONCILE_INTERVAL_SECONDS:
            # asyncio.to_thread: update_pending_reviews() is a blocking
            # call (sqlite3 + requests.post). Running it directly here
            # would freeze the entire event loop - including the HA
            # adapter's listen() task - for the duration of the HTTP
            # call. A stalled Supervisor could block device detection
            # for up to REQUEST_TIMEOUT_SECONDS. Offloading to a thread
            # keeps the loop responsive regardless.
            task = asyncio.create_task(asyncio.to_thread(update_pending_reviews, core.storage))
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
            elapsed_since_reconcile = 0
async def test_mock_device():
    """Test with mock device"""
    core = CognitiveCore()
    await core.start()

    test_device = {
        'id': 'shelly_123',
        'name': 'Shelly Pro 3EM',
        'manufacturer': 'Shelly',
        'area': 'Kitchen',
        'entities': [
            {'name': 'Shelly Total Power', 'domain': 'sensor'},
            {'name': 'Shelly Phase A', 'domain': 'sensor'},
        ]
    }

    mock_adapter = core.adapters['mock']
    await mock_adapter.send_test_device(
        core.on_device_detected,
        test_device
    )

    print("\n[CLI] Pending Review Cases:")
    cases = core.storage.get_pending_review_cases()
    for i, case in enumerate(cases, 1):
        print(
            f"[{i}] {case['device_name'] or case['device_id']} - "
            f"{case['hypothesis_category']} "
            f"({case['hypothesis_confidence']:.0%})"
        )

# Known classifier output categories. Used to validate manual corrections
# entered in the CLI so a typo doesn't silently poison the training data.
# NOTE: this list must stay in sync with the categories the classifiers
# in src/classifiers/ actually return. If a new classifier is added,
# add its category here too.
KNOWN_CATEGORIES = {
    "motion_sensor",
    "environmental_sensor",
    "energy_meter",
}

# TODO(STEP 4+): show_review_queue() currently mixes the CLI (print/input)
# with the data layer (Storage calls) in one function. This is fine at
# CLI-only scale. Once a Web UI or REST API is added, split this into:
#   - a pure function that yields pending review cases + accepts decisions
#   - a thin CLI adapter that only handles print/input
# so the UI and CLI can share the same review logic without duplicating it.
def show_review_queue():
    """CLI: Human Review workflow (STEP 5).

    Operates on review_cases via Storage.get_pending_review_cases()/
    resolve_review_case(). This is the current, primary review workflow -
    one entry per logical (device, classifier, hypothesis_category) case,
    not one entry per raw observation row.
    """
    storage = Storage()

    print("\n[CLI] Pending Review Cases:")
    cases = storage.get_pending_review_cases()

    if not cases:
        print("[CLI] No pending review cases")
        return

    for i, case in enumerate(cases, 1):
        print(f"\n[{i}] {case['device_name'] or case['device_id']}")
        print(f"    Classifier: {case['classifier_name']}")
        print(f"    Category: {case['hypothesis_category']}")
        print(f"    Confidence: {case['hypothesis_confidence']:.0%}")
        print(f"    Reasoning: {case['hypothesis_reasoning']}")
        print(f"    Case ID: {case['case_id']}")

        choice = input("    [a]pprove / [r]eject / [c]orrect / [s]kip > ").lower()

        if choice == 'a':
            result = storage.resolve_review_case(
                case['case_id'], expected_observation_id=case['observation_id'],
                decision='approved',
            )
            if result == 'resolved':
                print("    ✓ Approved")
                update_pending_reviews(storage)
            elif result == 'stale':
                print("    ⚠ This case changed since it was loaded (new observation "
                      "arrived). Skipped - re-run review to see the current state.")
            elif result == 'not_found':
                print("    ! Review case no longer exists. Skipped.")
        elif choice == 'r':
            result = storage.resolve_review_case(
                case['case_id'], expected_observation_id=case['observation_id'],
                decision='rejected',
            )
            if result == 'resolved':
                print("    ✗ Rejected")
                update_pending_reviews(storage)
            elif result == 'stale':
                print("    ⚠ This case changed since it was loaded. Skipped.")
            elif result == 'not_found':
                print("    ! Review case no longer exists. Skipped.")
        elif choice == 'c':
            print(f"    Known categories: {', '.join(sorted(KNOWN_CATEGORIES))}")
            corrected_category = input("    Correct category > ").strip()
            if not corrected_category:
                print("    ! No category entered, skipping")
            elif corrected_category not in KNOWN_CATEGORIES:
                print(f"    ! Unknown category '{corrected_category}', skipping")
                print(f"    ! Must be one of: {', '.join(sorted(KNOWN_CATEGORIES))}")
            else:
                result = storage.resolve_review_case(
                    case['case_id'], expected_observation_id=case['observation_id'],
                    decision='corrected', corrected_category=corrected_category,
                )
                if result == 'resolved':
                    update_pending_reviews(storage)
                    print(f"    ~ Corrected to '{corrected_category}'")
                elif result == 'stale':
                    print("    ⚠ This case changed since it was loaded. Skipped.")
                elif result == 'not_found':
                    print("    ! Review case no longer exists. Skipped.")
        elif choice == 's':
            print("    ~ Skipped")
def show_review_count():
    """CLI: Print the number of active review cases awaiting review.

    STEP 4A.1 / STEP 5: Foundation for review workflow notifications.
    """
    storage = Storage()
    count = storage.count_pending_reviews()
    print(f"Pending reviews: {count}")
def show_review_stats():
    """CLI: Print review analytics summary.

    STEP 4B.2: Thin presentation layer over Storage.get_review_analytics().
    No SQL here - all aggregation logic lives in Storage, so any future
    consumer (Web UI, REST API) can reuse the exact same data without
    duplicating queries.

    NOTE (STEP 5): These numbers are derived from classification_observations
    history (raw rows), not from review_cases (active cases) - see
    get_review_analytics()'s docstring for why these are intentionally
    two different metrics.
    """
    storage = Storage()
    analytics = storage.get_review_analytics()
    summary = analytics["summary"]

    print("Review Analytics")
    print()
    print(f"Observations: {summary['total_observations']}")
    print(f"Pending: {summary['pending']}")
    print(f"Reviewed: {summary['reviewed']}")
    print()
    print(f"Approved: {summary['approved']}")
    print(f"Rejected: {summary['rejected']}")
    print(f"Corrected: {summary['corrected']}")

    if summary["approval_rate"] is None:
        print("Approval rate: N/A (no reviews yet)")
    else:
        print(f"Approval rate: {summary['approval_rate']:.1%}")

    if analytics["by_classifier"]:
        print()
        print("By classifier:")
        for c in analytics["by_classifier"]:
            if c["approval_rate"] is None:
                print(f"- {c['classifier_name']}: no reviews yet")
            else:
                print(
                    f"- {c['classifier_name']}: "
                    f"{c['approved']}/{c['reviewed']} approved "
                    f"({c['approval_rate']:.1%})"
                )

    if analytics["most_corrected_categories"]:
        print()
        print("Most corrected categories:")
        for entry in analytics["most_corrected_categories"]:
            print(f"- {entry['hypothesis_category']}: {entry['correction_count']} correction(s)")

    if analytics["problematic_devices"]:
        print()
        print("Problematic devices:")
        for entry in analytics["problematic_devices"]:
            name = entry["device_name"] or entry["device_id"]
            print(f"- {name}: {entry['incorrect_count']} incorrect classification(s)")
def show_correction_patterns():
    """CLI: Print correction patterns derived from human review decisions.

    STEP 7 (first use of get_correction_patterns(), introduced passively
    in STEP 6 / deployed in STEP 6.1): shows where humans most often
    correct a classifier's guess, so a person can decide whether a
    classifier rule needs manual improvement.

    Read-only and purely informational. Calling this does not change any
    classifier behavior, confidence, or rule - Cognitive Core does not
    act on this data by itself. See docs/STEP5_ARCHITECTURE.md's "Non-Goals"
    section: information flows up to the human, authority never flows
    down from the system.

    Can raise RuntimeError if get_correction_patterns() detects an
    integrity violation (more than one labelled observation for the same
    logical review case) - that is intentional and surfaces here as-is,
    rather than being silently swallowed, since it means the underlying
    data can no longer be trusted without investigation.
    """
    storage = Storage()
    patterns = storage.get_correction_patterns()

    if not patterns:
        print("No correction patterns yet.")
        return

    print("Correction Patterns")
    print()
    for p in patterns:
        print(f"{p['classifier_name']} / {p['hypothesis_category']}")
        print(f"  corrected to: {p['corrected_category']}")
        print(f"  {p['correction_count']} of {p['sample_size']} reviewed case(s)")
        print()
if __name__ == '__main__':
    if len(sys.argv) > 1:
        command = sys.argv[1]

        if command == 'review':
            show_review_queue()
        elif command == 'review-count':
            show_review_count()
        elif command == 'review-stats':
            show_review_stats()
        elif command == 'correction-patterns':
            show_correction_patterns()
        elif command == 'test':
            asyncio.run(test_mock_device())
    else:
        core = CognitiveCore()
        try:
            asyncio.run(main_loop(core))
        except KeyboardInterrupt:
            print("\n[CORE] Shutting down...")
