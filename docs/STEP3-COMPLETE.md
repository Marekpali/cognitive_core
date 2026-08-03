# STEP 3 — Human Learning Loop: Complete

**Status:** Closed
**Release:** `v0.3.0`
**Date:** 2026-08-03

---

## What Was Built

STEP 3 turns STEP 2's observation logging into a functioning feedback loop between the classifiers and a human reviewer.

```
Home Assistant event
        ↓
Classifier runs
        ↓
Observation logged (STEP 2)
        ↓
Human review (STEP 3)
        ↓
Decision stored in SQLite
        ↓
(future: feeds back into classifier quality)
```

### STEP 3A — Schema Migration

- Idempotent migration in `Storage._ensure_review_columns()`
- Adds six columns to `classification_observations` if missing:
  `review_status`, `human_decision`, `reviewed_at`, `review_reason`,
  `corrected_category`, `reviewed_by`
- The final migration implementation is wrapped in try/except with
  rollback. Earlier rebuild testing exposed a partial migration
  (`Added column: review_status` followed by an interrupted restart
  before the remaining columns were added), which the idempotent
  design safely completed on the next startup with no manual
  intervention needed
- Verified across multiple real restarts, including that observed
  mid-migration interruption during add-on rebuilds

### STEP 3B — Data Layer

- `Storage.get_pending_reviews(limit=50, include_reviewed=False)` —
  single source of truth for pending observations, built for reuse by
  CLI, future UI, and stats without duplicated SQL
- Legacy `get_pending_reviews()` (review_queue/assets architecture)
  renamed to `get_pending_asset_reviews()` to avoid collision; call
  sites in `main.py` updated accordingly

### STEP 3C — CLI Learning Loop

- `python -m src.main review` — primary workflow, operates on
  `classification_observations`
- `python -m src.main legacy-review` — old review_queue/assets
  workflow, preserved for backward compatibility, not primary anymore
- `Storage.approve_observation(id, reason=None, reviewed_by="human")`
- `Storage.reject_observation(id, reason=None, reviewed_by="human")`
- `Storage.correct_observation(id, corrected_category, reason=None, reviewed_by="human")`
  — stores the human-supplied correct category separately from the
  classifier's original guess (`hypothesis_category` is never
  overwritten), so both values remain available for future accuracy
  tracking
- CLI validates `corrected_category` against a known-categories set
  before writing, rejecting typos rather than silently poisoning data
- `rowcount` checked before `commit()` in all three decision methods:
  an unknown `observation_id` rolls back and returns `False` without
  touching unrelated rows

---

## Verified End-to-End

- Migration confirmed idempotent across real restarts (`All review
  columns already present` on clean re-run)
- Migration correctly adds only missing columns when schema is
  partially ahead (observed: 2 columns added when 4 already existed
  from earlier testing, not a full re-migration)
- `docker exec ... python -m src.main review` resolved the relative
  `data/core.db` path to the same persistent database the add-on
  itself uses, because the container's working directory is `/app`.
  This was verified directly via `Storage().db_path` inspection, but
  it is an implicit dependency on working directory rather than an
  explicit one — recorded below as technical debt
- STEP 2 pipeline (classifiers, HA authentication, WebSocket
  subscription) unaffected by any STEP 3 change

---

## Known Limitations (Not Blockers)

- `Storage()` does not itself read `DB_PATH` from the environment —
  it relies on the process's working directory being `/app` (set by
  `run.sh` and the Dockerfile's `WORKDIR`) to land on the correct
  relative path. This has worked correctly in every tested case, but
  is coincidental rather than explicit. Worth making `Storage`
  DB-path-aware directly in STEP 4 to remove the implicit dependency.
- `show_review_queue()` in `main.py` mixes CLI I/O with data-layer
  calls in one function. Fine at CLI-only scale; flagged with a TODO
  for splitting into a pure decision function + thin CLI adapter once
  a Web UI or REST API is added.
- No statistics, accuracy tracking, or automated confidence tuning
  yet — deliberately out of scope for STEP 3.

---

## What's Next: STEP 4 — Review Analytics

The natural continuation is using the human decisions now being
collected to answer:

- Which classifier is most accurate?
- Which hypotheses get rejected most often?
- Which devices are consistently problematic to classify?
- Where do the heuristics need adjustment?

This is the point where Cognitive Core starts learning from its own
history, not just classifying against fixed rules.

---

## Backups

- `core.db.v0.2.0.backup` — pre-Learning-Loop snapshot
- `core.db.v0.3.0.backup` — post-STEP-3 snapshot, full review schema

---

*STEP 3 closure document. Written after verified end-to-end operation
on live production data.*
