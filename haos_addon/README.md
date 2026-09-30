# Cognitive Core

Home Assistant Add-on

## Purpose

Cognitive Core discovers Home Assistant devices, classifies them,
records classification observations, supports human review, and
provides review analytics while keeping the human in control.

## Current Status

- Device discovery through Home Assistant WebSocket
- Three device classifiers
- Classification observation logging
- Human review workflow: approve, reject, correct
- Review analytics
- Home Assistant pending-review sensor
- STEP 5 learning architecture defined

## Data

Database location: `/data/core.db`

Review sensor: `sensor.cognitive_core_pending_reviews`

## Safety

Human review remains the source of truth.

Analytics may support learning and future classifier improvements,
but Cognitive Core does not modify its own classification rules.

## Notes

Internal Home Assistant add-on for the Cognitive Core project.
