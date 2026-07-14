# tests/test_storage.py

import pytest
from pathlib import Path
import tempfile
from datetime import datetime

from src.storage import Storage

@pytest.fixture
def temp_storage():
    """Create temporary storage"""
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = Storage(Path(tmpdir) / "test.db")
        yield storage

def test_save_and_load_asset(temp_storage):
    """Test save/load"""
    
    asset = {
        'id': 'test_asset_1',
        'name': 'Test Device',
        'source_device_id': 'ha_123',
        'source_adapter': 'mock',
        'hypothesis': {
            'category': 'energy_meter',
            'confidence': 0.95,
            'reasoning': 'Test'
        },
        'lifecycle_state': 'provisional',
        'lifecycle_discovered_at': datetime.utcnow().isoformat() + 'Z'
    }
    
    asset_id = temp_storage.save_asset(asset)
    assert asset_id == 'test_asset_1'
    
    loaded = temp_storage.load_asset(asset_id)
    assert loaded is not None
    assert loaded['name'] == 'Test Device'

def test_review_queue(temp_storage):
    """Test review queue"""
    
    asset = {
        'id': 'test_asset_2',
        'name': 'Review Test',
        'hypothesis': {
            'category': 'energy_meter',
            'confidence': 0.85
        },
        'lifecycle_state': 'provisional',
        'lifecycle_discovered_at': datetime.utcnow().isoformat() + 'Z'
    }
    
    temp_storage.save_asset(asset)
    review_id = temp_storage.add_to_review_queue('test_asset_2')
    
    reviews = temp_storage.get_pending_reviews()
    assert len(reviews) == 1
    
    temp_storage.approve_review(review_id)
    
    reviews = temp_storage.get_pending_reviews()
    assert len(reviews) == 0
