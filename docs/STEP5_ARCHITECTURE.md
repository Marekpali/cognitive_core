# STEP 5 — Learning Architecture

**Status:** Draft for discussion — no code written against this yet
**Date:** 2026-08-03
**Precondition:** STEP 4 complete (`v0.4.0`) — Review Queue, Analytics, and the
Home Assistant sensor are live and verified on production data.

---

## Goal

Human-supervised learning: use accumulated review decisions
(`classification_observations` + `human_decision`) to help a human
developer improve Cognitive Core's classifiers — without the system
ever changing its own classification rules autonomously.

---

## The Closed Loop (as of v0.4.0)

```
Home Assistant
      │
      ▼
Device detected
      │
      ▼
Classifier
      │
      ▼
Observation (SQLite)
      │
      ▼
Human review (approve / reject / correct)
      │
      ▼
Analytics (get_review_analytics)
      │
      ▼
Home Assistant sensor
```

STEP 5 extends this loop by one more stage:

```
Analytics
      │
      ▼
Learning  ← new in STEP 5
```

"Learning" here means: turning accumulated decisions into something a
human can act on. It does not mean the classifiers change themselves.

---

## Five Levels of "Learning" (naming what we mean, precisely)

Each level is a distinct, separately-scoped future step. STEP 5 covers
Levels 1–2 only. Levels 3–5 are explicitly out of scope until stated
otherwise.

| Level | Name | Example | In scope for STEP 5? |
|---|---|---|---|
| **1** | Statistics | `motion_sensor → environmental_sensor: 37 corrections` | ✅ Yes |
| **2** | Knowledge | "MotionClassifier's false positives are more common among devices that expose humidity-related entities" | ✅ Yes |
| **3** | Rule analysis | "Rule #7 in MotionClassifier has an 82% false-positive rate" | ❌ Not yet |
| **4** | Recommendation | "Suggested patch: add `humidity > 0` check before classifying as motion_sensor" | ❌ Not yet |
| **5** | Autonomous action | System opens a pull request or self-modifies a classifier | ❌ Not yet — may never be in scope |

Level 1 is a straightforward extension of what `get_review_analytics()`
already does (STEP 4B). Level 2 requires correlating corrections with
observation metadata (device attributes, entity counts, etc.) — this
is new ground for STEP 5.

Levels 3–5 require Cognitive Core to reason about *why* a classifier
was wrong (rule-level introspection, not just outcome statistics).
That's a different kind of system and a different kind of risk profile
— it is not being ruled out permanently, but it does not start until
Levels 1–2 have been used long enough to know what's actually useful.

---

## Input

- `classification_observations` table (unchanged schema from STEP 4)
- Specifically the fields already present: `hypothesis_category`,
  `corrected_category`, `human_decision`, `classifier_name`,
  `device_id`, `device_name`, `device_model`, `device_manufacturer`,
  `device_entity_count`, `created_at`, `reviewed_at`

No new input sources. No new tables planned for STEP 5 Level 1–2 work
— this stays entirely inside the existing schema, unless production
data shows that repeated derived queries are too expensive or require
persisted snapshots, in which case that tradeoff gets evaluated
explicitly when it arises rather than assumed away here.

---

## Human Decisions (already captured, STEP 3–4)

- `approved` — classifier was right
- `rejected` — classifier was wrong, no correct answer supplied
- `corrected` — classifier was wrong, human supplied the right category

---

## Derived Knowledge (what STEP 5 computes)

- **Correction patterns** — `(hypothesis_category → corrected_category)`
  pairs with counts. This is the concrete gap identified during STEP 4
  closure: `most_corrected_categories` currently reports *how often* a
  category was corrected, not *what it was corrected to*. STEP 5's
  first piece of code addresses exactly this gap.
- **Classifier accuracy trend** — accuracy per classifier, but over
  time (e.g. weekly buckets), not just a single all-time number. This
  is what lets someone see "is this classifier getting better or
  worse," not just "what is its accuracy right now" (which STEP 4B
  already answers).
- **Device hotspots** — STEP 4B's `problematic_devices` already covers
  this at Level 1. STEP 5 may enrich it with device attributes
  (manufacturer, model, entity count) to look for shared
  characteristics among frequently-wrong devices — that enrichment is
  Level 2.

---

## Outputs (STEP 5 deliverables, Levels 1–2 only)

- **Learning report** — human-readable summary, likely a CLI command
  in the same style as `review-stats`, showing correction patterns and
  accuracy trends since a given date.
- **Export** — machine-readable dump (JSON or CSV) of observations +
  decisions, for use outside Cognitive Core (feeding into Claude, GPT,
  NotebookLM, spreadsheet analysis, or a future ML pipeline). Export
  is a data dump, not a recommendation — it makes no claims about what
  the data means.

Recommendation *text* generation (Level "4" in the table above, e.g.
"consider adding a humidity check") is explicitly **not** a STEP 5
output. Producing a correction-pattern statistic is Level 1. Turning
that statistic into prose advice about a specific rule change requires
Level 3 (rule-level introspection) as a prerequisite, which STEP 5
does not build.

---

## Non-Goals (explicit, load-bearing)

- **No automatic rule changes.** Classifiers are never modified by
  Cognitive Core itself, at any point in STEP 5.
- **No self-modifying classifiers.** This applies indefinitely, not
  just "for now" — any future change to this constraint is itself an
  architecture decision requiring the same level of deliberation as
  this document, not an incremental code change.
- **Human always approves changes.** Every classifier rule change,
  now and for the foreseeable future, is a human editing
  `src/classifiers/*.py` directly, informed by — but never automated
  by — Cognitive Core's own output.

The control flow is, and stays:

```
Human
  ↑
Recommendation / Report / Export
  ↑
Analytics
  ↑
Review
```

Information flows up to the human. Authority never flows down from
the system.

---

## Sequencing (once this document is agreed)

STEP 5 will follow the same rhythm as STEP 3 and STEP 4: one small
piece → test on real SQLite → deploy → verify on production → commit.
No multi-part batches in a single session.

The first concrete piece of code, once this document is settled, is:

```
Storage.get_correction_patterns()
```

returning `(hypothesis_category, corrected_category, count)` triples
— the Level 1 gap identified during STEP 4 closure. Everything else in
this document (accuracy trends, device enrichment, report CLI, export)
is sequenced *after* that, each as its own small step, not decided or
built today.

---

## Open Questions (to resolve before or during implementation, not now)

- Report cadence: on-demand CLI only, or also a scheduled/cron
  variant? (STEP 4A's precedent — event-driven + periodic reconcile —
  may or may not apply here; not yet decided.)
- Export format: JSON, CSV, or both? Single flat file, or one export
  per table/view?
- Time-bucketing for accuracy trends: daily, weekly, or configurable?
- Whether "device hotspots" enrichment (Level 2) needs new columns, or
  can be computed entirely from existing `device_model` /
  `device_manufacturer` / `device_entity_count` fields already stored
  per observation.

These are deliberately left open — they get decided when the specific
piece of work is scoped, not speculatively here.

---

*This document is the reference point for STEP 5. Code that doesn't
trace back to something written here should prompt a question before
being written, not after.*
