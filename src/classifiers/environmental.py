# src/classifiers/environmental.py

class EnvironmentalSensorClassifier:
    """Environmental sensor classifier for multi-sensor devices (temp, humidity, illuminance, motion)"""
    
    async def classify(self, device: dict) -> dict:
        """Classify environmental sensor"""
        score = 0
        reasoning = []
        
        # Manufacturer patterns
        manufacturer = (device.get('manufacturer') or '').lower()
        if 'tuya' in manufacturer or 'tze' in manufacturer:
            score += 0.85
            reasoning.append('Manufacturer: Tuya/TZE')
        elif 'zigbee' in manufacturer.lower():
            score += 0.70
            reasoning.append('Manufacturer: Generic Zigbee')
        
        # Model/name patterns - TS0601 is Tuya multi-sensor
        model = (device.get('model') or '').lower()
        name = (device.get('name') or '').lower()
        
        if 'ts0601' in model or 'ts0601' in name:
            score += 0.95
            reasoning.append('Model: TS0601 (Tuya multi-sensor)')
        
        # Entities - look for environmental sensor markers
        entities = device.get('entities', []) or []
        entity_names = [(e.get('name') or '').lower() for e in entities]
        entity_types = [(e.get('type') or '').lower() for e in entities]
        
        # Count environmental indicators
        has_temperature = any('temperature' in name or 'temp' in name for name in entity_names)
        has_humidity = any('humidity' in name for name in entity_names)
        has_illuminance = any('illuminance' in name or 'lux' in name or 'light' in name for name in entity_names)
        has_motion = any('motion' in name or 'occupancy' in name or 'presence' in name for name in entity_names)
        has_battery = any('battery' in name for name in entity_names)
        
        sensor_count = sum([has_temperature, has_humidity, has_illuminance, has_motion, has_battery])
        
        if sensor_count >= 3:
            score += 0.90
            reasoning.append(f'Multi-sensor: {sensor_count} sensor types detected')
        elif sensor_count == 2:
            score += 0.70
            reasoning.append(f'Multi-sensor: {sensor_count} sensor types detected')
        elif sensor_count == 1:
            score += 0.40
            reasoning.append(f'Single sensor type detected')
        
        # Device class hints
        device_classes = []
        if has_temperature:
            device_classes.append('temperature')
        if has_humidity:
            device_classes.append('humidity')
        if has_illuminance:
            device_classes.append('illuminance')
        if has_motion:
            device_classes.append('motion')
        if has_battery:
            device_classes.append('battery')
        
        # Final score calculation
        final_score = min(score / 3, 1.0)
        
        if final_score > 0.4:
            return {
                'category': 'environmental_sensor',
                'confidence': final_score,
                'reasoning': ' + '.join(reasoning),
                'classifier': 'EnvironmentalSensorClassifier',
                'device_classes': device_classes
            }
        
        return None
