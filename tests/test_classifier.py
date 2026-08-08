# tests/test_classifier.py

import pytest
import asyncio

from src.classifiers.energy import EnergyMeterClassifier

@pytest.fixture
def classifier():
    return EnergyMeterClassifier()

@pytest.mark.asyncio
async def test_classify_shelly(classifier):
    """Test Shelly classification"""
    
    device = {
        'name': 'Shelly Pro 3EM',
        'manufacturer': 'Shelly',
        'area': 'Kitchen',
        'entities': [
            {'name': 'Power', 'domain': 'sensor'},
            {'name': 'Energy', 'domain': 'sensor'},
        ]
    }
    
    hypothesis = await classifier.classify(device)
    
    assert hypothesis is not None
    assert hypothesis['category'] == 'energy_meter'
    assert hypothesis['confidence'] >= 0.8

@pytest.mark.asyncio
async def test_classify_non_meter(classifier):
    """Test non-meter device"""
    
    device = {
        'name': 'Living Room Light',
        'manufacturer': 'Philips',
        'area': 'Living Room',
        'entities': [
            {'name': 'On/Off', 'domain': 'light'},
        ]
    }
    
    hypothesis = await classifier.classify(device)
    
    assert hypothesis is None or hypothesis['confidence'] < 0.5
