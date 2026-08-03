class EnvironmentalSensorClassifier:
    """Classify environmental and multi-sensor devices."""

    CATEGORY = "environmental_sensor"
    MIN_CONFIDENCE = 0.55

    async def classify(self, device: dict) -> dict | None:
        """Return an environmental-sensor classification or None."""
        score = 0.0
        reasoning: list[str] = []

        manufacturer = str(device.get("manufacturer") or "").lower()
        model = str(device.get("model") or "").lower()
        name = str(device.get("name") or "").lower()

        entities = device.get("entities") or []
        if not isinstance(entities, list):
            entities = []

        searchable_entities: list[str] = []

        for entity in entities:
            if not isinstance(entity, dict):
                continue

            searchable_entities.append(
                " ".join(
                    str(entity.get(field) or "").lower()
                    for field in (
                        "name",
                        "entity_id",
                        "type",
                        "domain",
                        "device_class",
                        "original_device_class",
                    )
                )
            )

        def entity_matches(*terms: str) -> bool:
            return any(
                any(term in entity_text for term in terms)
                for entity_text in searchable_entities
            )

        has_temperature = entity_matches("temperature", "temp")
        has_humidity = entity_matches("humidity")
        has_illuminance = entity_matches("illuminance", "lux")
        has_motion = entity_matches(
            "motion",
            "occupancy",
            "presence",
        )
        has_battery = entity_matches("battery")

        environmental_classes = [
            label
            for label, detected in (
                ("temperature", has_temperature),
                ("humidity", has_humidity),
                ("illuminance", has_illuminance),
                ("motion", has_motion),
                ("battery", has_battery),
            )
            if detected
        ]

        primary_sensor_count = sum(
            (
                has_temperature,
                has_humidity,
                has_illuminance,
                has_motion,
            )
        )

        # Entity evidence should carry the greatest weight.
        if primary_sensor_count >= 3:
            score += 0.70
            reasoning.append(
                f"Detected {primary_sensor_count} environmental sensor types"
            )
        elif primary_sensor_count == 2:
            score += 0.55
            reasoning.append("Detected 2 environmental sensor types")
        elif primary_sensor_count == 1:
            score += 0.25
            reasoning.append("Detected 1 environmental sensor type")

        if has_battery:
            score += 0.05
            reasoning.append("Battery-powered device")

        # Manufacturer is supporting evidence only.
        if "tuya" in manufacturer or "_tze" in manufacturer:
            score += 0.10
            reasoning.append("Manufacturer indicates Tuya/TZE")

        # TS0601 is too broad to be decisive by itself.
        if "ts0601" in model or "ts0601" in name:
            score += 0.10
            reasoning.append("Model family TS0601")

        confidence = min(score, 1.0)

        if confidence < self.MIN_CONFIDENCE:
            return None

        return {
            "category": self.CATEGORY,
            "confidence": round(confidence, 3),
            "reasoning": " + ".join(reasoning),
            "classifier": self.__class__.__name__,
            "device_classes": environmental_classes,
        }
