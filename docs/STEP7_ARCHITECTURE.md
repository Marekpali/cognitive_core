# STEP 7 — Precedent Memory (Architecture Decision Record)

**Status:** Accepted — no implementation code written against this yet
**Date:** 2026-09-28 (drafted and accepted, with refinements);
**revised 2026-10-03** after STEP 7P and after the implementation plan
(`docs/STEP7A_IMPLEMENTATION_PLAN.md`); see *Revision 2026-10-03* at the end
**Scope of this document:** STEP 7a (shadow mode) only
**Precondition:** `7p-active` — human decisions (D0), raw hypothesis and
observation identity (D1) are immutable in production; observations are
written by the gated sweep of STEP 7P (`docs/STEP7_OBSERVATION_SOURCES.md`),
each linked to its stored input by `input_id`. `review_cases` is the only
Human Review workflow; `get_correction_patterns()` exists (STEP 6) and is
deployed with zero callers (STEP 6.1).

---

## Decision in one paragraph

Cognitive Core gains a **precedent annotation layer**: after a classifier
hypothesis is logged, Core records — separately — what past human
decisions suggest about that hypothesis. In STEP 7a this annotation is
**shadow only**: it changes nothing the system does. Its sole purpose is
to be measured against later human decisions, so that any future
authority threshold (STEP 7b) is derived from observed data rather than
chosen up front.

---

## Why this needs an ADR

`docs/STEP5_ARCHITECTURE.md` states:

> Information flows up to the human. Authority never flows down from
> the system.

and requires any change to its Non-Goals to be "an architecture decision
requiring the same level of deliberation as this document."

**STEP 7a does not change that principle.** Annotations are information
recorded for later human evaluation; they carry no authority. This ADR
exists anyway because STEP 7a introduces the first component whose
*purpose* is to become a decision input one day. Drawing the boundary
explicitly now — before any code exists — is what keeps 7b from
happening by accident.

---

## The pipeline (to be preserved beyond STEP 7)

```
classifier input                   (STEP 7P: canonical snapshot + fingerprint, append-only)
  → raw classifier hypothesis      (immutable, forever; linked by input_id)
  → precedent annotation           (STEP 7a: recorded, no effect)
  → decision policy                (future, separate ADR — does not exist yet)
```

Never:

```
classifier hypothesis → "corrected" classifier hypothesis
```

At any point in time it must be answerable, independently:

0. What did the classifier see? → `classification_inputs`, reached through
   `classification_observations.input_id` (immutable; `NULL` on
   observations written before STEP 7P — the annotator must accept that)
1. What did the classifier originally conclude? → `classification_observations`
2. What had Core learned from humans at that moment? → `precedent_annotations`
3. What did a policy do with that? → (future) decision-policy log
4. What did the human finally decide? → `review_cases` / `human_decision`

---

## Two kinds of memory — kept separate

| | **Device precedent** | **Class pattern** |
|---|---|---|
| Question it answers | "What did a human decide about *this* device, for *this* hypothesis?" | "What do humans usually decide when *this classifier* says *this category*, on *other* devices?" |
| Lookup key | exact `(device_id, classifier_name, hypothesis_category)` with a resolved `review_case` | `(classifier_name, hypothesis_category)` |
| Evidence | exactly one human decision | all independent labelled logical cases, **excluding this device** |
| Nature | **recall** (remembering) | **prediction** (generalising) |
| Verifiable in 7a? | **No — by construction** (see below) | Yes, when the annotated case is later resolved |

The two are **never combined into one score**, in storage or in reports.
Each observation gets at most one annotation per memory type, written
once and **never refreshed** (see *When an annotation is written*).

They also have **different metrics**, because they are different kinds of
claim:

- **Device precedent → recall metrics only**: how often Core remembered a
  prior human decision (annotations generated, cases annotated). No
  accuracy is computed for it.
- **Class pattern → prediction metrics**: verifiable / matched /
  direction-only / mismatched / ambiguous.

A combined "precedent accuracy" figure across both memory types is not
to be computed anywhere.

**Leave-one-out for class patterns.** A class-pattern annotation for
device D excludes D's own decisions from its evidence. Otherwise the
two memories would silently overlap and a single human decision would be
counted as both "what we know about D" and "what we know in general."

### Finding: device precedents cannot be validated in STEP 7a

A device precedent only exists when its logical key already has a
resolved `review_case`. STEP 5 guarantees a resolved case **never
reopens** — so no later human decision will ever be made for that key.
Device-precedent annotations are therefore memory recall, not
predictions, and `precedent-report` must present them as such (counted,
never scored). This is not a defect: it is the correct consequence of
STEP 5's no-reopen guarantee, stated explicitly so nobody later reads
"0 verified device precedents" as a bug.

---

## What STEP 7a must NOT change

Each item below gets a regression test that compares behaviour with the
annotation layer enabled vs. disabled:

- raw classifier hypothesis (`hypothesis_category`, `hypothesis_confidence`,
  `hypothesis_reasoning`) in `classification_observations`, and its
  `input_id`
- `classification_inputs` rows (snapshot, fingerprint, outcome,
  `matched_classifiers`)
- the fingerprint gate: which devices a sweep evaluates and which it
  reports `unchanged`
- `classification_sweeps` rows: counts and `devices_json`; an annotator
  failure must never turn a device's outcome into `error`
- review routing: which `review_cases` are created, refreshed, pending
- `sensor.cognitive_core_pending_reviews`
- what the human sees in the `review` CLI (see *Blind review* below)

### Immutability enforced by the database, not by convention

- `classification_observations`: a column-scoped
  `BEFORE UPDATE OF <raw columns>` trigger aborts any update to the raw
  classifier fields only:
  `observation_group_id`, `device_id`, `classifier_name`,
  `hypothesis_category`, `hypothesis_confidence`, `hypothesis_reasoning`,
  `device_name`, `device_model`, `device_manufacturer`,
  `device_source_adapter`, `device_entity_count`, `created_at`.
  **The row is not frozen.** Workflow/review columns (`review_status`,
  `human_decision`, `corrected_category`, `reviewed_at`, `review_reason`,
  `reviewed_by`) remain writable — `resolve_review_case()` dual-write is
  unaffected. **As implemented in M1 (2026-09-29):** value-change
  semantics — one trigger per column, `BEFORE UPDATE OF <col> ... WHEN
  OLD.<col> IS NOT NEW.<col>` (NULL-safe), so re-assigning the same value
  (`SET col = col`) is allowed and only a real change is aborted, with
  `IMMUTABLE_RAW_HYPOTHESIS: classification_observations.<col> cannot be
  changed after insert` (`sqlite3.IntegrityError`). One trigger per column
  because SQLite `RAISE()` accepts only a literal message. No code path
  updates raw columns (verified against all `UPDATE
  classification_observations` statements in `src/storage.py`).
- `classification_observations.id`: protected by a separate trigger,
  `trg_obs_immutable_id` (`IMMUTABLE_OBSERVATION_IDENTITY: ...`), same
  value-change semantics. Kept out of the raw-hypothesis list on purpose:
  record identity immutable, raw hypothesis immutable, review metadata
  mutable. Changing an id would orphan `review_cases.last_observation_id`
  and precedent annotation references.
- `classification_observations.input_id`: `trg_obs_immutable_input_id`
  (STEP 7P), same value-change semantics.
- `precedent_annotations`, `precedent_annotation_evidence` and
  `precedent_audit`: append-only; `BEFORE UPDATE` and `BEFORE DELETE` triggers abort, with
  the STEP 7P convention — names `trg_<table>_no_update` /
  `trg_<table>_no_delete`, message
  `APPEND_ONLY: <table> rows cannot be updated|deleted`.

The raw-column and identity triggers are in production since D1, the
STEP 7P triggers since D2. The new precedent triggers change the expected
trigger set: `d2_verify.py` / `active_verify.py` report any other trigger
as *unexpected*, so the 7a deployment needs its own cumulative verifier
(new file; the old verifiers are not edited).

---

## Blind review (decision: annotations are NOT shown to the reviewer)

If the reviewer sees "Core suspects: occupancy" before deciding, the
human decision is no longer independent of the annotation, and every
"correct" count in STEP 7a becomes partly self-fulfilling (anchoring).
Therefore, during STEP 7a:

- the `review` CLI does **not** display annotations;
- annotations are visible only through `precedent-report`.

Revisit when moving to 7b, where showing the annotation may itself be
the intended behaviour.

---

## Temporal integrity

- An annotation may use only human decisions with
  `decided_at < annotation.created_at`. The newest such timestamp is
  stored as `evidence_as_of`.
- An annotation is **verifiable** only against a decision with
  `decided_at > annotation.created_at`.
- **No backfill for resolved cases.** A case that was already resolved
  when the layer starts never gets an annotation for its judged
  observation. Backfill is possible in principle (evidence cut at
  `observation.created_at`) but is exactly where leakage bugs hide.

### When an annotation is written

**Normal path (`annotation_trigger = 'observation'`).** For every
observation written after the layer has started: in the same transaction
that writes the observation (see *Components* and *Failure isolation*).
The annotation is the knowledge available at the moment of observation. It is **never refreshed** when more decisions
arrive later, there are no versions, and `UNIQUE(observation_id,
memory_type)` stays.

**Bootstrap exception for legacy pending cases
(`annotation_trigger = 'bootstrap_pending'`).** STEP 7P writes an
observation only when a device's fingerprint changes, so the cases that
were `pending` before 7a (18 in production on 2026-10-03) would otherwise
never be annotated and never become verifiable. For those, and only
those:

- *Eligible:* a `pending` case whose `expected_observation_id` row was
  created before `precedent_layer_started_at` and has no class-pattern
  annotation.
- *The cut-off `precedent_layer_started_at`* is the `created_at` of the
  single `layer_started` row in `precedent_audit`. It is written by Core
  at its first effective start with `precedent_mode: shadow`, before any
  observation is written in that mode — never during a run in `off`, so
  a deployment with the layer off is not the start of the layer. It
  survives restarts and a temporary return to `off`, and is immutable
  (partial unique index + append-only triggers). The resolver never
  writes it: without the row the layer has not started and nothing is
  bootstrapped.
- *When:* inside `resolve_review_case()`, on the first effective
  `pending → resolved`: **after** the `expected_observation_id` (stale)
  check and the pending guard pass, **before** the human decision is
  written.
- *Evidence:* only decisions resolved earlier
  (`decided_at < annotation.created_at`). The decision being recorded can
  never be evidence for its own annotation; its `decided_at` is strictly
  later than `annotation.created_at` (enforced in code, tested).
- *Human decision > precedent annotation.* The annotation is computed
  before the decision is written and stored in the **same transaction,
  inside a `SAVEPOINT`**. If computing or writing the annotation or its
  evidence fails, only the savepoint is rolled back: **the human decision
  is still committed**. The annotation is all-or-nothing — never a
  partial annotation or evidence set. Such a case stays without a
  bootstrap annotation for good (no later backfill) and is recorded as
  `annotation_failed`.
- *`annotation_failed` is an audit state, not a kind of annotation.* It
  is a row in `precedent_audit` (see *Data model*), written on **both**
  paths with `source` = `observation` or `bootstrap_pending`, counted in
  the report; `annotation_trigger` keeps exactly two values. Writing the
  audit row has its own savepoint and can block neither a decision nor
  a sweep.
- *Memory type:* class pattern only. A pending case has no resolved
  precedent for its own key, so there is no device precedent to recall.
- *No evidence is not a failure.* If no earlier decision exists for the
  case's `(classifier_name, hypothesis_category)` on other devices
  (`sample_size = 0`), no annotation is written, as on the normal path;
  that is not `annotation_failed`.
- *Blind review holds:* the annotation is created after the reviewer has
  entered the decision and is never displayed.

Consequence: among legacy cases that share a
`(classifier_name, hypothesis_category)`, resolving them one after another
lets the second use the decision on the first, the third use the first
two, and so on, instead of all of them sharing one frozen picture built
from the two decisions that existed before 7a.

**The bootstrap cohort is not the normal temporal cohort.** Its evidence
cut-off lies at decision time, later than the observation, so these
annotations do not show what Core knew *when it observed*. They carry
their own `annotation_trigger` value and are reported in their own
section; they are never merged into the figures of the normal cohort.

### One canonical resolver — no bypass

After 7a is deployed, **every production path from `pending` to
`resolved` goes through `resolve_review_case()`**. The deprecated
`approve_observation()` / `reject_observation()` / `correct_observation()`
must either delegate to it or not be reachable as a production resolution
path. Otherwise a decision could be recorded without the bootstrap step
and silently fall out of the 7a data. (Today the `review` CLI in
`src/main.py` calls only `resolve_review_case()`; the legacy methods
still write decisions with their own SQL.)

Decided: the legacy methods **delegate**. For the current observation of
a pending case they call `resolve_review_case()`; for an observation that
is already decided they return `already_resolved` as before; for anything
else they return the new result `not_reviewable` and write nothing. An
explicit break of any old out-of-repo use is preferred over a side road
around the resolver. A static and a runtime test prove they contain no
SQL of their own and that there is no bypass.

---

## Unit of evaluation (lesson from STEP 6)

Raw observation rows are not independent evidence (production: 10 rows
behind 1 human judgment). Therefore:

- **Annotations are written per observation** (cheap, complete audit trail).
- **Verification counts one annotation per resolved logical case**: the
  annotation attached to the observation the human actually judged —
  `classification_observations.id = expected_observation_id` at
  resolution time (the row that carries `human_decision`). All other
  annotations for that case count toward "generated" but never toward
  correct/incorrect.

---

## Data model

Three new tables; existing tables are not changed (their immutability
triggers are in production since D1/D2).

```sql
CREATE TABLE precedent_annotations (
    id                    TEXT PRIMARY KEY,
    observation_id        TEXT NOT NULL REFERENCES classification_observations(id),
    memory_type           TEXT NOT NULL,   -- 'device_precedent' | 'class_pattern'
    annotation_trigger    TEXT NOT NULL,   -- 'observation' | 'bootstrap_pending'

    -- copied logical key, for joins without going through review_cases
    device_id             TEXT NOT NULL,
    classifier_name       TEXT NOT NULL,
    hypothesis_category   TEXT NOT NULL,

    -- what the memory suggests
    result                TEXT NOT NULL,   -- 'suggestion' | 'ambiguous'
    suggested_outcome     TEXT,            -- 'approved' | 'rejected' | 'corrected'; NULL when ambiguous
    suggested_category    TEXT,            -- only when suggested_outcome = 'corrected'

    -- the evidence, recorded raw (never only a label)
    evidence_count        INTEGER NOT NULL, -- decisions supporting the suggestion (0 when ambiguous)
    sample_size           INTEGER NOT NULL, -- all independent labelled cases considered
    correction_count      INTEGER NOT NULL, -- decisions with human_decision = 'corrected'
    outcome_distribution  TEXT NOT NULL,    -- JSON, e.g. {"approved":1,"corrected:occupancy":3}
    evidence_maturity     TEXT NOT NULL,    -- descriptive label only, see below
    evidence_as_of        TEXT NOT NULL,
    evidence_digest       TEXT NOT NULL,    -- SHA256 of the canonical evidence snapshot

    policy_version        TEXT NOT NULL,    -- version of the annotation logic
    created_at            TEXT NOT NULL,

    UNIQUE(observation_id, memory_type)
);

-- Immutable evidence snapshot: one row per human decision that fed the annotation.
CREATE TABLE precedent_annotation_evidence (
    annotation_id         TEXT NOT NULL REFERENCES precedent_annotations(id),
    review_case_id        TEXT,            -- NULL if the decision predates review_cases
    labelled_observation_id TEXT NOT NULL, -- the row carrying human_decision
    device_id             TEXT NOT NULL,
    human_decision        TEXT NOT NULL,   -- value COPIED at annotation time
    corrected_category    TEXT,            -- value COPIED at annotation time
    decided_at            TEXT NOT NULL,   -- value COPIED at annotation time
    PRIMARY KEY (annotation_id, labelled_observation_id)
);
```

No row is written when a memory type has no evidence at all
(`sample_size = 0`); report denominators come from
`classification_observations`, not from this table.

No `input_id` column: the input of an annotated observation is reached
through `observation_id → classification_observations.input_id`, which is
immutable.

One audit table carries both the layer cut-off and annotation failures:

```sql
CREATE TABLE precedent_audit (
    id             TEXT PRIMARY KEY,
    event          TEXT NOT NULL CHECK (event IN ('layer_started', 'annotation_failed')),
    source         TEXT CHECK (source IN ('observation', 'bootstrap_pending')),
    observation_id TEXT,
    review_case_id TEXT,
    error_class    TEXT,
    error_message  TEXT,
    policy_version TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    CHECK (
        (event = 'layer_started'     AND source IS NULL)
     OR (event = 'annotation_failed' AND source IS NOT NULL
         AND source IN ('observation', 'bootstrap_pending'))
    )
);
CREATE UNIQUE INDEX idx_precedent_layer_started
    ON precedent_audit(event) WHERE event = 'layer_started';
```

Append-only like the annotation tables. It is an audit only: nothing
reads it except the report and the verifier.

A correction rate is not stored as a float: `correction_count` and
`sample_size` are stored, and any ratio is derived at report time (see
*Small-sample presentation*).

### Ties → `ambiguous`, never an arbitrary pick

For class patterns, the suggestion is the modal outcome of
`outcome_distribution` (outcomes are `approved`, `rejected`, and
`corrected:<category>` as separate buckets). If two or more buckets
share the top count (e.g. `2 × corrected:occupancy`,
`2 × corrected:environmental`), the annotation is written with
`result = 'ambiguous'`, `suggested_outcome = NULL`,
`suggested_category = NULL`, and the full distribution. Shadow mode
never breaks ties.

`rejected` has no alternative category: a class pattern dominated by
rejections suggests "the classifier is probably wrong here" without
naming a replacement.

### Evidence provenance

Every annotation must be explainable months later without consulting
the then-current database state: *"Why did Cognitive Core produce this
annotation at that time?"*

When this ADR was written (2026-09-28) human decisions were mutable: a
resolved case could be resolved again. D0 closed that (2026-09-29,
`docs/D0-DEPLOYMENT.md`): a second decision returns `already_resolved` on
both the case path and the legacy methods, and nothing is overwritten.

The value-copying snapshot is kept regardless: it makes an annotation
self-contained — explainable without joins into tables whose protection
is enforced by application code — and is what `evidence_digest` is
computed over. `precedent_annotation_evidence` stores a **copy of the
decision values** as they were when the annotation was produced, together
with the identifiers, and `evidence_digest` is a SHA256 over the canonical
(sorted, JSON-serialised) evidence rows plus `policy_version`. The digest
lets a report detect if the evidence table was ever tampered with, and
lets two annotations be recognised as built on identical evidence.

Leave-one-out is recorded implicitly: no evidence row for a
class-pattern annotation may carry the annotated observation's
`device_id` — enforced by test, and checkable from the stored snapshot.

Re-resolution of a resolved case is refused since D0; the snapshot design
does not depend on it.

### `evidence_maturity` is descriptive, not authority

`insufficient | emerging | established` describes **how much evidence
exists**, not whether Core should be trusted. The bands: `insufficient`
< 3, `emerging` 3–9, `established` ≥ 10 independent labelled cases
(`sample_size`). The label is **purely a function of that count**: no
branch, ranking or decision may depend on it, and the report always
shows the real `n` next to it. It is produced by
`describe_evidence_maturity()` — deliberately not named
`classify_strength()` or anything that sounds like a decision.

In STEP 7a the label must not affect, directly or indirectly:

- classifier confidence
- the fingerprint gate or any sweep outcome
- review routing
- any automatic action

The bands live in versioned code (`policy_version`), and the raw numbers
are always stored next to the label, so bands can be revised later and
historical annotations re-labelled at report time without rewriting
history. No band is to be treated as the 7b threshold; operational
thresholds are an output of 7a's data, decided in the 7b ADR.

---

## Components

- **`src/learning/precedent.py`** — pure functions, no I/O:
  - `evaluate_device_precedent(key, resolved_decision) -> Annotation | None`
  - `evaluate_class_pattern(key, decisions_excluding_device) -> Annotation | None`
  - `describe_evidence_maturity(sample_size) -> str`
  - `evidence_digest(evidence_rows, policy_version) -> str`
- **`Storage`** — `get_precedent_evidence(key, as_of)` (reads decisions
  directly from `classification_observations` with
  `human_decision IS NOT NULL`, same integrity rules as
  `get_correction_patterns()`), `log_precedent_annotation(...)` (writes
  the annotation and its evidence rows atomically, inside the caller's
  savepoint),
  `get_precedent_report()`.
- **`ObservationSweep._persist()`** (`src/sweep.py`, active mode only) —
  one transaction per evaluated device: input → observations → review
  cases → the annotator in a `SAVEPOINT`, once per observation written.
  Its result is **ignored**; the device's outcome, the sweep counts and
  the gate are not touched. In `observation_mode: shadow` no observation
  is written, so the annotator does not run.
- **`Storage.resolve_review_case()`** — the bootstrap exception only (see
  *When an annotation is written*).
- **CLI `precedent-report`** — see below.
- **Switch** — add-on option `precedent_mode: off | shadow` in
  `config.yaml`, read by `src/options.py` from `/data/options.json` like
  `observation_mode`, so the layer can be switched without a rebuild.
  Default, and any missing, unreadable or unknown value: **off**. After
  the first `shadow` start, `off` is an emergency mode: observations
  written while it is off are knowingly never annotated afterwards.

### Failure isolation

**Normal path.** Input, observations and review cases are written in one
transaction and the annotator runs inside it, in its own `SAVEPOINT`.
Any failure — including the `RuntimeError` integrity check shared with
`get_correction_patterns()` — rolls back that savepoint only and leaves
an `annotation_failed` row (`source = 'observation'`); the transaction
commits normally. It never causes a lost observation or input, a missed
review case, a device outcome of `error`, or a stalled sweep. Because
everything commits together there is no window in which an observation
exists with neither an annotation nor an audit row, and none in which a
case could be resolved between its creation and its normal annotation.

**Bootstrap path.** Same principle, different mechanism: the annotation
shares the decision's transaction but lives in its own `SAVEPOINT`. A
failure rolls back the savepoint only; the human decision is committed,
nothing partial remains, and the case is recorded as `annotation_failed`
(see *When an annotation is written*). Precedent memory is information
without authority: on no path can it stop, delay or alter a human
decision.

Performance: evidence is small today, so direct SQL per observation is
acceptable in 7a. Caching is deferred until measured as necessary
(consistent with the STEP 5 doc's "evaluate the tradeoff when it
arises" rule).

---

## `precedent-report` (not a single accuracy number)

Separate sections, never merged: device precedent, class pattern
(normal cohort, `annotation_trigger = 'observation'`), and class pattern
— bootstrap cohort (`'bootstrap_pending'`, same metrics, own table, with a
note that its evidence cut-off is the decision time, not the observation
time). `annotation_failed` counts are shown per `source`. Every maturity
label is shown with its `n`.

**Device precedent (recall):**

| Metric | Meaning |
|---|---|
| annotations generated | rows written |
| logical cases annotated | distinct `(device, classifier, category)` |

With an explicit note that these are recall and are not scored.

**Class pattern (prediction):**

| Metric | Meaning |
|---|---|
| annotations generated | rows written |
| logical cases annotated | distinct `(device, classifier, category)` |
| ambiguous | annotations with `result = 'ambiguous'` (never scored) |
| verifiable | annotated cases later resolved by a human, per the temporal and unit rules above |
| matched | suggestion equals decision (and category, for `corrected`) |
| direction-only | suggestion said "wrong" (corrected/rejected) and the human agreed it was wrong, but chose a different category or rejected |
| mismatched | everything else |
| not yet verifiable | still pending |

Plus a per-annotation listing for every verified case:
`device, classifier, category → suggestion (evidence_count/sample_size,
correction_count/sample_size, evidence_maturity) → human decision →
match class`.

### Small-sample presentation

Raw counts are always shown. Ratios are presented as fractions
(`2/3`, `4/5`); a percentage is added **only when the denominator is
≥ 5** (e.g. `7/9 (78%)`). A `1/1` must never render as "100%". This
applies to every ratio in the report, including per-annotation evidence
ratios, not only the headline metrics.

---

## Explicitly out of scope for STEP 7a

- Any change to confidence, category, inputs, the fingerprint gate,
  review routing, or the HA sensor (that is 7b, separate ADR).
- Auto-applying a human decision to repeat observations (7c or later).
- Cross-key device knowledge ("this device was corrected under another
  classifier, so…") — that is Level 2 knowledge per
  `STEP5_ARCHITECTURE.md`.
- Modifying classifiers in any way (unchanged Non-Goal, indefinitely).
- Backfilling annotations onto observations of already resolved cases;
  refreshing or versioning an existing annotation.
- Behavioural evidence (own ADR, later).

---

## Completion criteria (STEP 7a)

1. This ADR reviewed and accepted.
2. Regression tests prove every item in *What STEP 7a must NOT change*
   is identical with the annotation layer on vs. off.
3. DB triggers enforce raw-hypothesis immutability (column-scoped) and
   append-only annotations/evidence; tests prove a raw-column UPDATE is
   aborted **and** a review-column UPDATE still succeeds.
4. Every annotation stores raw counts, `evidence_as_of`,
   `policy_version`, an evidence snapshot with copied decision values,
   and a verifiable `evidence_digest`; a test re-resolves a contributing
   case, gets `already_resolved`, and proves the stored snapshot is
   unchanged.
5. Ties produce `result = 'ambiguous'` with no suggested outcome or
   category.
6. Leave-one-out and temporal cut-off are covered by tests (a device's
   own decision never appears in its class-pattern evidence; a decision
   made after the annotation never appears in it).
7. Annotator failure (incl. integrity `RuntimeError`) on the normal path
   is tested not to affect inputs, observations, review cases, sweep
   outcomes/counts, or the HA handler; it leaves no partial annotation
   and exactly one `annotation_failed` row. Input, observations, cases
   and annotation commit in one transaction (tested with an injected
   crash before the commit).
7a. Bootstrap: tests prove (i) a failing annotator (while computing,
   and while writing the annotation or its evidence) leaves the human
   decision committed, **no** partial annotation or evidence row, and an
   `annotation_failed` audit record; (ii) the decision being recorded is
   never in its own evidence; (iii) resolving legacy cases in sequence
   grows the evidence of the later ones; (iv) a stale or already-resolved
   call writes no annotation; (v) only eligible legacy cases are
   bootstrapped, and the cut-off `precedent_layer_started_at` is never
   written in `off`, written once at the first `shadow` start, and
   unchanged by a restart or a return to `off`; (vi) with the layer off nothing is
   annotated and resolution works; (vii) no bypass: every production
   path `pending → resolved` passes the canonical resolver.
7b. An existing annotation is never refreshed: later decisions leave it
   byte-identical (and the triggers refuse UPDATE/DELETE).
8. `precedent-report` implements the metrics above, with separate
   sections per memory type and a separate section for the bootstrap
   cohort, no combined accuracy, and fraction-only ratios below a
   denominator of 5.
9. `review` CLI verified not to display annotations (blind review).
10. Deployed to HAOS as a FULL GATE on the D2 pattern
    (`docs/D2-DEPLOYMENT.md`): package from git objects, negative control,
    cumulative verifier (D0 + D1 + D2 + 7a), attestation, then tag.
11. **Exit condition for 7a is time + data, not code**: 7a runs until
    `precedent-report` shows enough verified class-pattern cases to
    argue a threshold. That number is itself a decision for the 7b ADR.

---

## Decisions recorded during review (2026-09-28)

- Ties → `result = 'ambiguous'`, no suggestion (was open question).
- Ratios as fractions below n = 5, percentage added from n ≥ 5 (was open question).
- Strength renamed to `evidence_maturity`, descriptive only.
- Device precedent = recall; separate metrics; no combined accuracy.
- Evidence provenance stored as a value-copying snapshot + digest.
- Immutability trigger is column-scoped, not row-freezing.
- Accepted findings: device precedent is not verifiable under STEP 5's
  lifecycle; STEP 7a review is blind; class patterns are leave-one-device-out.

## Open questions

1. ~~Maturity bands.~~ **Closed:** < 3 / 3–9 / ≥ 10, see
   *`evidence_maturity` is descriptive*.
2. ~~Production DB copy for the trigger pre-check.~~ **Closed:** SQLite
   backup API inside the container, copied out with `docker cp` / read
   over Samba (D0–D2).
3. ~~Re-resolution of resolved cases.~~ **Closed by D0:** refused.
4. ~~Physical storage of `precedent_layer_started_at` and of the
   `annotation_failed` audit record.~~ **Closed:** `precedent_audit`.

## Revision 2026-10-03 (after STEP 7P; no implementation yet)

Reviewed against the closed STEP 7P (`7p-active`). The principle, the two
memories, blind review, temporal integrity, ties, the evidence snapshot
and the report are unchanged. Changed:

1. Hook point: `ObservationSweep._persist()` instead of the removed
   `CognitiveCore.classify_device()`; no annotator in shadow mode.
2. *Must NOT change*: `best_hypothesis` and asset writes removed (gone
   since 7P); inputs, `input_id`, the gate and sweep rows added.
3. Pipeline starts at the stored classifier input; no `input_id` column on
   annotations (reached through the observation).
4. Evidence-provenance rationale updated for D0; open question 3 closed.
5. Trigger section: `trg_obs_immutable_input_id`, STEP 7P naming and
   message convention, new cumulative verifier; open question 2 closed.
6. Kill switch is an add-on option, default off on any doubt.
7. Precondition `7p-active`; deployment as a FULL GATE on the D2 pattern.
8. **Bootstrap exception for legacy pending cases** (decided 2026-10-03):
   annotation created inside `resolve_review_case()` immediately before
   the first decision, evidence = earlier decisions only; own
   `annotation_trigger`, own report section. Stated explicitly:
   **no backfill for resolved cases, no refresh of an existing
   annotation, bootstrap cohort ≠ normal temporal cohort.**
9. **Human decision > precedent annotation**: the bootstrap annotation is
   written in a `SAVEPOINT` of the decision's transaction; its failure
   never blocks the decision, leaves nothing partial and is audited as
   `annotation_failed` (an audit state, not an `annotation_trigger`).
10. **One canonical resolver**: no production resolution path may bypass
    `resolve_review_case()`; legacy methods delegate or are unreachable.
11. **From the accepted implementation plan:** `annotation_failed` is
    recorded on both paths, with `source`; one `precedent_audit` table
    holds it and the `layer_started` cut-off, which is written only at
    the first `shadow` start and never in `off`; option
    `precedent_mode: off | shadow`; the normal path annotates inside the
    `_persist()` transaction via `SAVEPOINT`; legacy methods delegate
    with the new result `not_reviewable`; maturity bands fixed as a pure
    function of the evidence count. Open questions 1 and 4 closed.
