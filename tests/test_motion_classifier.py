import pytest
from src.classifiers.motion import MotionSensorClassifier


@pytest.fixture
def classifier():
    return MotionSensorClassifier()


@pytest.mark.asyncio
async def test_motion_class_only(classifier):
    """Pure motion device — device_class=motion only"""
    device = {
        "name": "Hallway Sensor",
        "entities": [
            {
                "entity_id": "binary_sensor.hallway_1",
                "domain": "binary_sensor",
                "device_class": "motion"
            }
        ]
    }
    result = await classifier.classify(device)
    assert result is not None
    assert result["category"] == "motion_sensor"
    assert result["confidence"] == 0.85  # 60 + 25 = 85
    assert "device_class=motion" in result["reasoning"]


@pytest.mark.asyncio
async def test_motion_with_keyword(classifier):
    """Motion + occupancy keyword"""
    device = {
        "name": "Motion Detector",
        "entities": [
            {
                "entity_id": "binary_sensor.occupancy_sensor",
                "domain": "binary_sensor",
                "device_class": "motion"
            }
        ]
    }
    result = await classifier.classify(device)
    assert result is not None
    assert result["confidence"] == 1.0  # 60 + 25 + 15 = 100
    assert "occupancy" in result["reasoning"]


@pytest.mark.asyncio
async def test_binary_sensor_no_motion_class(classifier):
    """Binary sensor WITHOUT device_class=motion — should NOT classify"""
    device = {
        "name": "Door Sensor",
        "entities": [
            {
                "entity_id": "binary_sensor.door_open",
                "domain": "binary_sensor",
                "device_class": "door"
            }
        ]
    }
    result = await classifier.classify(device)
    assert result is None


@pytest.mark.asyncio
async def test_keyword_without_device_class(classifier):
    """Keyword 'motion' but no device_class=motion — should NOT classify"""
    device = {
        "name": "Motion Light",
        "entities": [
            {
                "entity_id": "light.motion_light",
                "domain": "light",
                "device_class": None
            }
        ]
    }
    result = await classifier.classify(device)
    assert result is None


@pytest.mark.asyncio
async def test_motion_with_temperature(classifier):
    """Motion + temperature — still classifies as motion (not environmental)"""
    device = {
        "name": "Occupancy Sensor",
        "entities": [
            {
                "entity_id": "binary_sensor.occupancy",
                "domain": "binary_sensor",
                "device_class": "motion"
            },
            {
                "entity_id": "sensor.temperature",
                "domain": "sensor",
                "device_class": "temperature"
            }
        ]
    }
    result = await classifier.classify(device)
    assert result is not None
    assert result["category"] == "motion_sensor"
