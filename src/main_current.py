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
def show_review_queue():
    """CLI: Review queue"""
    storage = Storage()
    
    print("\n[CLI] Pending Reviews:")
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
        elif command == 'test':
            asyncio.run(test_mock_device())
    else:
        core = CognitiveCore()
        try:
            asyncio.run(main_loop(core))
        except KeyboardInterrupt:
            print("\n[CORE] Shutting down...")
