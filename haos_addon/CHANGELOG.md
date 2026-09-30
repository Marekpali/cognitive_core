# Changelog

## 0.4.0 — 2026-08-03

- Review analytics: `Storage.get_review_analytics()` with summary,
  per-classifier breakdown, most-corrected categories, and
  problematic devices
- CLI: `review-stats`
- STEP 5 learning architecture documented (`docs/STEP5_ARCHITECTURE.md`
  in the project repository)

## 0.3.0 — 2026-08-03

- Human review workflow: `review` CLI command with approve, reject,
  correct decisions
- Idempotent schema migration for review columns
  (`review_status`, `human_decision`, `reviewed_at`, `review_reason`,
  `corrected_category`, `reviewed_by`)
- `Storage.count_pending_reviews()` and `Storage.get_pending_reviews()`
- Home Assistant sensor: `sensor.cognitive_core_pending_reviews`,
  published event-driven (on startup, on device detection, on review
  decisions) with a 60-second reconcile safety net
- Fixed `DB_PATH` resolution so the CLI (via `docker exec`) and the
  main process always read/write the same persistent database
- Fixed a SQLite thread-safety issue between the async event loop and
  `asyncio.to_thread()`-based sensor publishing

## 0.2.0 — 2026-08-01

- Migrated from a standalone Docker container to a native Home
  Assistant Add-on
- Authentication via `SUPERVISOR_TOKEN` and the Supervisor WebSocket
  proxy (`ws://supervisor/core/websocket`)
- Classification observation logging
  (`classification_observations` table)
- Three device classifiers: energy meter, environmental sensor,
  motion sensor
