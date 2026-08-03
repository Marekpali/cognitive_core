# src/main.py
import asyncio
import sys
from pathlib import Path
from src.core import CognitiveCore
from src.storage import Storage
async def main_loop(core: CognitiveCore):
    """Main event loop"""
    await core.start()
    
    # Listen for device signals
    for adapter in core.adapters.values():
        asyncio.create_task(adapter.listen(core.on_device_detected))
    
    # Keep running
    while True:
        await asyncio.sleep(1)
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
    
    print("\n[CLI] Review Queue:")
    reviews = core.storage.get_pending_asset_reviews()
    for i, review in enumerate(reviews, 1):
        print(
            f"[{i}] {review['name']} - "
            f"{review['hypothesis_category']} "
            f"({review['hypothesis_confidence']:.0%})"
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
#   - a pure function that yields pending observations + accepts decisions
#   - a thin CLI adapter that only handles print/input
# so the UI and CLI can share the same review logic without duplicating it.
def show_review_queue():
    """CLI: Learning Loop review queue (STEP 3C).

    Operates on classification_observations via Storage.get_pending_reviews().
    This is the current, primary review workflow.
    """
    storage = Storage()

    print("\n[CLI] Pending Observations:")
    observations = storage.get_pending_reviews()

    if not observations:
        print("[CLI] No pending observations")
        return

    for i, obs in enumerate(observations, 1):
        print(f"\n[{i}] {obs['device_name'] or obs['device_id']}")
        print(f"    Classifier: {obs['classifier_name']}")
        print(f"    Category: {obs['hypothesis_category']}")
        print(f"    Confidence: {obs['hypothesis_confidence']:.0%}")
        print(f"    Reasoning: {obs['hypothesis_reasoning']}")
        print(f"    Observation ID: {obs['id']}")

        choice = input("    [a]pprove / [r]eject / [c]orrect / [s]kip > ").lower()

        if choice == 'a':
            storage.approve_observation(obs['id'])
            print("    ✓ Approved")
        elif choice == 'r':
            storage.reject_observation(obs['id'])
            print("    ✗ Rejected")
        elif choice == 'c':
            print(f"    Known categories: {', '.join(sorted(KNOWN_CATEGORIES))}")
            corrected_category = input("    Correct category > ").strip()
            if not corrected_category:
                print("    ! No category entered, skipping")
            elif corrected_category not in KNOWN_CATEGORIES:
                print(f"    ! Unknown category '{corrected_category}', skipping")
                print(f"    ! Must be one of: {', '.join(sorted(KNOWN_CATEGORIES))}")
            else:
                storage.correct_observation(obs['id'], corrected_category)
                print(f"    ~ Corrected to '{corrected_category}'")
        elif choice == 's':
            print("    ~ Skipped")
def show_legacy_review_queue():
    """CLI: Legacy review queue (STEP 1 architecture).

    Operates on review_queue + assets via Storage.get_pending_asset_reviews().
    Kept for backward compatibility. Not the primary workflow anymore -
    use 'review' for the Learning Loop instead.
    """
    storage = Storage()
    
    print("\n[CLI] Pending Reviews (legacy):")
    reviews = storage.get_pending_asset_reviews()
    
    if not reviews:
        print("[CLI] No pending reviews")
        return
    
    for i, review in enumerate(reviews, 1):
        print(f"\n[{i}] {review['name']}")
        print(f"    Category: {review['hypothesis_category']}")
        print(f"    Confidence: {review['hypothesis_confidence']:.0%}")
        print(f"    Reasoning: {review['hypothesis_reasoning']}")
        print(f"    Review ID: {review['review_id']}")
        
        choice = input("    [y]es / [n]o / [s]kip > ").lower()
        
        if choice == 'y':
            storage.approve_review(review['review_id'])
            print("    ✓ Approved")
        elif choice == 'n':
            storage.reject_review(review['review_id'])
            print("    ✗ Rejected")
        elif choice == 's':
            print("    ~ Skipped")
if __name__ == '__main__':
    if len(sys.argv) > 1:
        command = sys.argv[1]
        
        if command == 'review':
            show_review_queue()
        elif command == 'legacy-review':
            show_legacy_review_queue()
        elif command == 'test':
            asyncio.run(test_mock_device())
    else:
        core = CognitiveCore()
        try:
            asyncio.run(main_loop(core))
        except KeyboardInterrupt:
            print("\n[CORE] Shutting down...")
