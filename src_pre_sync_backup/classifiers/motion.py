class MotionSensorClassifier:
    async def classify(self, device: dict) -> dict | None:
        score = 0
        reasoning = []
        entities = device.get("entities", []) or []
        
        has_motion_class = any(
            entity.get("device_class") == "motion"
            for entity in entities
        )
        
        if has_motion_class:
            score += 60
            reasoning.append("device_class=motion")
        
        if any(entity.get("domain") == "binary_sensor" for entity in entities):
            score += 25
            reasoning.append("binary_sensor domain")
        
        entity_names = " ".join(
            str(entity.get("entity_id", "")).lower()
            for entity in entities
        )
        
        for keyword in ("motion", "occupancy", "pir"):
            if keyword in entity_names:
                score += 15
                reasoning.append(f"keyword '{keyword}' in entity names")
                break
        
        if not has_motion_class:
            return None
        
        if score >= 60:
            return {
                "category": "motion_sensor",
                "confidence": min(score / 100, 1.0),
                "reasoning": ", ".join(reasoning),
                "classifier": "MotionSensorClassifier",
            }
        
        return None
