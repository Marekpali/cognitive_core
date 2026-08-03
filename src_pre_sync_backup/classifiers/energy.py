# src/classifiers/energy.py
class EnergyMeterClassifier:
    """Energy meter classifier"""
    async def classify(self, device: dict) -> dict:
        """Classify energy meter"""
        score = 0
        reasoning = []
        # Manufacturer
        manufacturer = (device.get('manufacturer') or '').lower()
        if 'shelly' in manufacturer:
            score += 0.95
            reasoning.append('Manufacturer: Shelly')
        elif 'tuya' in manufacturer:
            score += 0.80
            reasoning.append('Manufacturer: Tuya')
        # Entities
        entities = device.get('entities', []) or []
        entity_names = [(e.get('name') or '').lower() for e in entities]
        has_power = any('power' in name for name in entity_names)
        has_energy = any('energy' in name for name in entity_names)
        if has_power or has_energy:
            score += 0.85
            reasoning.append('Has power/energy sensors')
        # Name
        device_name = (device.get('name') or '').lower()
        if 'meter' in device_name or '3em' in device_name:
            score += 0.70
            reasoning.append('Name suggests meter')
        # Final
        final_score = min(score / 3, 1.0)
        if final_score > 0.4:
            return {
                'category': 'energy_meter',
                'confidence': final_score,
                'reasoning': ' + '.join(reasoning),
                'classifier': 'EnergyMeterClassifier'
            }
        return None