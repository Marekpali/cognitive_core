# STEP 7a — implementation plan

**Status:** PLAN — accepted 2026-10-03 with the corrections below
incorporated. No implementation, no production change.
**Date:** 2026-10-03
**Implements:** `docs/STEP7_ARCHITECTURE.md` @ `9b4e94f` (closed ADR).
**Production today:** `8570f77`, `observation_mode: active` (`7p-active`);
18 review cases `pending`, 2 `resolved` — the 18 stay undecided until the
layer is deployed **and** enabled.

Decisions this plan makes (and only these): storage of the layer cut-off
and of the `annotation_failed` audit; call flow and transaction boundaries;
migration, triggers, verifier, tests; deployment.

---

## 1. Data model

Three new tables. No existing table, column or trigger changes.

### 1.1 `precedent_annotations`, `precedent_annotation_evidence`

Exactly as in the ADR, including `annotation_trigger TEXT NOT NULL CHECK
(annotation_trigger IN ('observation','bootstrap_pending'))`,
`memory_type` CHECK `('device_precedent','class_pattern')`, `result` CHECK
`('suggestion','ambiguous')`, `UNIQUE(observation_id, memory_type)`.
Index `precedent_annotations(device_id, classifier_name,
hypothesis_category)`.

### 1.2 `precedent_audit` — one mechanism for the cut-off and for failures

```sql
CREATE TABLE precedent_audit (
    id             TEXT PRIMARY KEY,
    event          TEXT NOT NULL CHECK (event IN ('layer_started', 'annotation_failed')),
    source         TEXT CHECK (source IN ('observation', 'bootstrap_pending')),
    observation_id TEXT,
    review_case_id TEXT,
    error_class    TEXT,
    error_message  TEXT,            -- truncated to 200 chars; never printed by verifiers
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

- **`precedent_layer_started_at`** = `created_at` of the single
  `layer_started` row. **When it is written:** by Core, at its first
  effective start with `precedent_mode: shadow`, **before** the adapter
  connects and therefore before any observation is written in that mode
  (`INSERT OR IGNORE`). Never during D3 or any other run in `off`: the
  deployment with the layer off is not the start of the layer. Once
  written it stays for good — also across a temporary return to `off` —
  guaranteed by the partial unique index (no second row) and the
  append-only triggers (the first is immutable).
  The review CLI never writes it: a resolver running in `shadow` without
  the row treats the layer as not started (no bootstrap). The enable gate
  therefore verifies the row exists before any case is reviewed.
- **`annotation_failed`** — one row per failed attempt, on **both** paths,
  `source` = `observation` or `bootstrap_pending`. Purely an audit: no
  code reads it except the report and the verifier.
- Why one table and not two: a layer start is an audit event like any
  other; one table means two triggers instead of four and one place to
  look. `annotation_trigger` on annotations stays two-valued.

### 1.3 Triggers (6 new, STEP 7P convention)

`trg_<table>_no_update`, `trg_<table>_no_delete` for the three tables,
message `APPEND_ONLY: <table> rows cannot be updated|deleted`. Total in
production afterwards: 13 (D1) + 5 (7P) + 6 (7a) = **24**.

### 1.4 Migration

`precedent_store.ensure_schema(cursor)`, idempotent (`CREATE … IF NOT
EXISTS`), called from `Storage.init_schema()` after
`init_step7p_schema()` — on every start, **regardless of the option**:
the schema exists while the function is off, as with 7P. Pure additions,
so the 7P code runs unchanged on the migrated database (rollback proof by
test, as for D1-on-D2).

---

## 2. Switch

Add-on option **`precedent_mode: off | shadow`**, default `off`
(`config.yaml`: `options.precedent_mode: "off"`, `schema: list(off|shadow)`).
`src/options.py: read_precedent_mode()` — same lookup as
`read_observation_mode()`; missing, unreadable or unknown → `off`.

`Storage(db_path, precedent_mode=None)`: `None` resolves through
`read_precedent_mode()`, so the review CLI (a separate `docker exec`
process) sees the same setting as Core.

---

## 3. Files

| File | Change |
|---|---|
| `src/learning/__init__.py`, `src/learning/precedent.py` | **new**, pure: `evaluate_device_precedent`, `evaluate_class_pattern` (modal bucket, ties → ambiguous), `describe_evidence_maturity`, `evidence_digest`, `POLICY_VERSION = "7a.1"` |
| `src/precedent_store.py` | **new**, SQL only: `ensure_schema`, `PRECEDENT_TRIGGERS`, `ensure_layer_started` (Core only), `layer_started_at`, `labelled_decisions(cursor, …, before)` (evidence query + integrity check), `insert_annotation`, `annotation_exists`, `insert_audit` |
| `src/precedent_annotator.py` | **new**, orchestration, both cursor-level and never raising: `annotate_in_transaction(cursor, observation_id)` (normal path), `bootstrap_in_transaction(cursor, case, observation_id)` (resolver) |
| `src/precedent_report.py` | **new**: report data + rendering (fractions below n = 5) |
| `src/storage.py` | `ensure_schema` call; `precedent_mode`; single-transaction write for `_persist()` with cursor-level input/observation/review-case helpers (§4.1); `resolve_review_case()` flow (§4.2); legacy methods delegate (§4.3). `storage.py` is already 1 592 lines — all new SQL lives in `precedent_store.py` |
| `src/sweep.py` | `_persist()` calls the single-transaction write (§4.1) |
| `src/core.py` | read option; in `shadow`, `ensure_layer_started` before the adapter connects |
| `src/options.py` | `read_precedent_mode()` |
| `src/main.py` | CLI `precedent-report` |
| `haos_addon/config.yaml` | option + schema |
| `scripts/d3_verify.py` | **new** cumulative verifier (§6) |
| `tests/…` | §5 |

---

## 4. Call flow and transactions

### 4.1 Normal path — `ObservationSweep._persist()`

One transaction per evaluated device, the same pattern as the bootstrap:

```
BEGIN IMMEDIATE
INSERT classification_inputs
INSERT classification_observations (one per match, input_id set)

SAVEPOINT review_cases                       # derived state, as today:
try:   upsert review case per observation    #   a failure here must not
       RELEASE                                #   lose input/observations
except Exception: ROLLBACK TO; RELEASE; log  #   (reconciled on next start)

if precedent_mode == shadow and the layer has started:
    for each observation:
        SAVEPOINT precedent_annotation
        try:   t = now
               device precedent : labelled decision for the SAME key, reviewed_at < t
               class pattern    : labelled decisions, same (classifier, category),
                                  OTHER devices, reviewed_at < t, one per device
               write annotation(s) + evidence rows (annotation_trigger = 'observation')
               RELEASE
        except Exception:
               ROLLBACK TO precedent_annotation; RELEASE
               SAVEPOINT precedent_audit
               try:   INSERT precedent_audit(annotation_failed, source='observation'); RELEASE
               except Exception: ROLLBACK TO; RELEASE; log only
COMMIT
```

- Input, observations, review cases and the annotation (or its
  `annotation_failed` row) become durable **together**. There is no
  window in which a crash leaves an observation with neither an
  annotation nor an audit row, and none in which a case could be resolved
  between its creation and its normal annotation.
- A 7a failure rolls back its savepoint only: the device's outcome is
  never `error` because of it, and counts, gate and review cases are
  unaffected.
- **Consequence for 7P code:** today `_persist()` commits the
  input + observations and then each review-case upsert separately. The
  single transaction replaces that for both `off` and `shadow` (one code
  path, so what is tested is what runs). Review-case failure isolation is
  kept by its own savepoint. `Storage` gets cursor-level variants of
  `record_classification_input` and `upsert_review_case`; the public
  `upsert_review_case()` keeps its behaviour. The on/off regression tests
  (§5) and a 7P equivalence test prove rows and sweep results are
  identical to `8570f77`.

### 4.2 Canonical resolver — `Storage.resolve_review_case()`

```
validate decision                                   (unchanged)
BEGIN IMMEDIATE                                     # write lock before any read
SELECT status, last_observation_id, key FROM review_cases WHERE id = ?
    none          -> ROLLBACK, 'not_found'
    not pending   -> ROLLBACK, 'already_resolved'
    pointer moved -> ROLLBACK, 'stale'              (same results and log lines as today)

if precedent_mode == shadow and the layer has started (row exists):
    if observation.created_at < layer_started_at
       and no class-pattern annotation for the observation:
        t_ann = now
        SAVEPOINT precedent_bootstrap
        try:   evidence = labelled decisions, same (classifier, category),
                          other devices, reviewed_at < t_ann
               if evidence: write annotation + evidence rows
                            (annotation_trigger = 'bootstrap_pending', created_at = t_ann)
               RELEASE precedent_bootstrap
        except Exception:
               ROLLBACK TO precedent_bootstrap; RELEASE precedent_bootstrap
               SAVEPOINT precedent_audit
               try:   INSERT precedent_audit(annotation_failed, source='bootstrap_pending')
                      RELEASE
               except Exception: ROLLBACK TO; RELEASE; log only

t_dec = now, forced strictly greater than t_ann
UPDATE review_cases … WHERE id = ? AND last_observation_id = ? AND status = 'pending'   (rowcount must be 1)
UPDATE classification_observations … WHERE id = ? AND human_decision IS NULL            (rowcount must be 1)
COMMIT
```

- `BEGIN IMMEDIATE` holds the write lock from the check to the commit, so
  the explicit check is as race-free as today's single guarded `UPDATE`
  (which stays, with its `WHERE`, as a second line of defence).
- The decision cannot be its own evidence for two independent reasons:
  it is not written yet when evidence is read, and class-pattern evidence
  excludes the case's own device.
- A failing annotator costs the savepoint only. If the decision itself
  fails afterwards, everything rolls back — including the audit row — and
  the case is still `pending` and still eligible.
- No evidence (`sample_size = 0`) → nothing written, not a failure.

### 4.3 No bypass — legacy methods

`approve_observation()` / `reject_observation()` / `correct_observation()`
keep their signature and `DecisionResult`, but stop writing with their own
SQL. `_record_legacy_decision()` becomes:

```
observation already decided                      -> 'already_resolved'   (as today)
observation unknown                              -> 'not_found'          (as today)
observation is the current evidence of a pending case
                                                 -> resolve_review_case(case, observation, …)
anything else                                    -> 'not_reviewable'     (new, falsy; nothing written)
```

After this, the only statement in `src/` that sets
`classification_observations.human_decision` or
`review_cases.status = 'resolved'` on an existing case is inside
`resolve_review_case()`. `d2_verify.check_d0` (which calls the legacy
methods on already-decided rows and expects `already_resolved`) keeps
passing.

`backfill_review_cases()` can create a case as `resolved` only from a
labelled observation that has no case; with the legacy write path gone
that cannot arise any more. It is left unchanged and covered by a test.

---

## 5. Tests

**Pure logic (`test_precedent.py`)** — modal outcome; ties → `ambiguous`
with no suggestion; `corrected:<category>` buckets; maturity bands as a
function of `sample_size` only; static test that no code outside
`describe_evidence_maturity()` and the report reads the label; digest
stable under row order and sensitive to any value and to `policy_version`.

**Schema (`test_precedent_store.py`)** — tables, CHECKs, 6 triggers with
exact messages; both `precedent_audit` CHECK branches; `layer_started`
written once, second insert ignored, unchanged after reopen; **never
written in `off`**, written by Core in `shadow` before the first sweep,
still present after a return to `off`; migration idempotent; 7P code on the migrated
database (rollback proof).

**Normal path (`test_precedent_annotator.py`)**
- annotation per new observation; device precedent and class pattern
  never merged; leave-one-device-out; temporal cut-off.
- no evidence → no row, no audit row.
- **no refresh:** a later decision leaves an existing annotation
  byte-identical; a second sweep writes none.
- **regression on/off:** inputs, observations, review cases, sweep counts
  and `devices_json` identical with the layer `shadow` vs `off`.
- **failure isolation:** annotator raising (while computing / while
  writing) → input, observations and review cases committed, sweep
  outcome and counts unchanged, no partial annotation, one
  `annotation_failed` row with `source = 'observation'`; audit insert
  failing too → still no effect.
- **one transaction:** a crash injected before `COMMIT` leaves no input,
  observation, case, annotation or audit row; after `COMMIT` every new
  observation has either its annotation, an `annotation_failed` row, or
  no evidence at all.
- **7P equivalence:** with the layer `off`, rows and sweep results equal
  those of `8570f77`; a failing review-case upsert still does not lose
  the input and observations.
- `observation_mode: shadow` → annotator never called.

**Bootstrap (`test_precedent_bootstrap.py`)**
- eligible legacy case → annotation `bootstrap_pending` and the decision,
  `annotation.created_at < decided_at`.
- **the 18:** a fixture shaped like production (2 resolved, 18 pending in
  three classifier groups) resolved in sequence — evidence grows within a
  group, the first case of a group without earlier decisions gets none.
- the decision being recorded is never in its own evidence.
- **decision outranks annotation:** annotator failing while computing /
  writing the annotation / writing an evidence row → decision committed,
  **zero** annotation and evidence rows, one `annotation_failed`
  (`bootstrap_pending`); audit insert failing too → decision committed.
- stale / already resolved / not found → nothing written at all.
- **no backfill:** cases resolved before the layer started get nothing,
  at start and on a repeated resolve call.
- observation created after the cut-off with no evidence at its time →
  **not** bootstrapped on resolution.
- layer `off` → resolution identical to today's, nothing in any precedent
  table, no `layer_started`.
- decision failing after a successful savepoint → no annotation remains.

**No bypass (`test_no_resolution_bypass.py`)**
- legacy methods on the current observation of a pending case → resolved
  through the resolver, bootstrap annotation present.
- legacy on a non-current observation → `not_reviewable`, nothing written.
- static: every statement in `src/` assigning `human_decision` or
  `status = 'resolved'` is inside `resolve_review_case()` (or the
  create-only branch of `backfill_review_cases()`).

**Report, options, config, blind review** — sections never merged,
bootstrap cohort separate with its `annotation_failed` count, fractions
below n = 5; `precedent_mode` fallback to `off`; `config.yaml` schema;
`review` CLI output contains no annotation text.

**Verifier (`test_d3_verify.py`)** — as for `d2_verify`: PASS, negative
control fails only in the new section, each tamper case, live database
byte-identical.

Existing suite (247) must stay green; coverage of the new modules ≥ 80 %.

---

## 6. `scripts/d3_verify.py` (cumulative, read-only on live)

| Section | Checks |
|---|---|
| D0, D1, PINS | reused from `d2_verify.py` |
| DATA | every `pre_d3.db` row still present; a human decision present in the snapshot is identical; an undecided observation/case may have been decided **once** (review fields only); a case pointer may only advance to evidence newer than the snapshot. (`d2_verify`'s rule "pre-D2 observations keep `input_id` NULL" does not apply to a post-7P snapshot.) |
| D2 | the 5 STEP 7P triggers, exact refusals |
| D3 | the 6 precedent triggers, tables, CHECKs; exact refusals on a copy; **no trigger outside the 24 expected** |
| RESOLVER | on a copy, deployed code: legacy methods cannot label a non-current observation; no decision path other than the resolver |
| LAYER | consistent with the option and the history: never started (D3) → no `layer_started`, 0 annotations, 0 audit rows; started → exactly one `layer_started` (also when the option is back to `off`), every annotation has its evidence rows, digest recomputed and equal, `created_at` < the decision it is verified against, no class-pattern evidence row with the annotation's own device, `bootstrap_pending` only on observations older than the cut-off, no annotation on an observation older than the cut-off except `bootstrap_pending` |
| REHEARSAL | **forecast, not a gate invariant.** On a copy, deployed code, layer forced to `shadow`: resolves every pending case in one **explicitly stated order** (the order the `review` CLI lists them; printed) and reports annotations written / without evidence / failed. Live untouched. The section fails only if the mechanism breaks (an exception escapes, a decision is not committed, a partial annotation remains) — never on the counts. A different real review order may give different numbers; that is not an error. The formal analysis uses the real `decided_at` and the evidence that preceded it, not the forecast |

`d2_verify.py` and `active_verify.py` are not edited; after D3 they report
the six new triggers as unexpected, by design.

---

## 7. Deployment — two gates, as for 7P

**D3 — code and schema, layer off (FULL GATE).** Phases as in D2:
package from git objects → baseline → `pre_d3.db` backup (never a file
named `config.*` in the tree; `find` check before rebuild) → negative
control with the old container (D0, D1, D2, PINS, DATA pass; D3,
RESOLVER, LAYER, REHEARSAL fail) → deploy `src/` +
`config.yaml` → rebuild → **start** → hashes → log
(`precedent_mode=off`, `STEP 7a schema verified (6 triggers)`, active
sweep `unchanged`) → `d3_verify` all PASS with `LAYER` never started
(**no `layer_started` row**) and the REHEARSAL forecast → attestation →
tag `d3-deployed` (= 7a deployed, **layer off, not started**).
Rollback: 7P source + config, rebuild; the three empty tables stay.
Deployment alone cannot bootstrap anything: with the layer off the
resolver behaves exactly as today.

**7a enable — lightweight FULL GATE, own GO.** `pre_7a.db` backup →
UI option `precedent_mode: shadow` (saving restarts the app by itself) →
log → `d3_verify`: exactly one `layer_started`, written before the first
sweep of that start, 0 annotations, 18 pending untouched → attestation →
tag `7a-shadow`. Only then are the 18 cases
reviewed through the `review` CLI; `precedent-report` and `d3_verify`
afterwards.
Rollback: option back to `off` — emergency mode, operational only.
Annotations, audit rows and the `layer_started` row stay; observations
written while `off` are knowingly never annotated afterwards.

---

## 8. Decisions taken (2026-10-03)

1. **`precedent_audit` as one table**, with the hard CHECK of §1.2, one
   partial unique index and the append-only triggers.
2. **`precedent_mode: off | shadow`**, default and every unrecognised
   value → `off`. `layer_started` is never written in `off`; only at the
   first effective start in `shadow`, before observations are written in
   that mode; it then stays for good.
3. **Legacy methods delegate**, new result `not_reviewable`: an explicit
   break of any old out-of-repo use is preferred over a side road around
   the resolver. Static + runtime test that they contain no SQL of their
   own.
4. **Maturity bands:** `insufficient` < 3, `emerging` 3–9, `established`
   ≥ 10 independent cases. **Purely a function of the amount of
   evidence.** No branch, ranking or decision may depend on the label;
   the report always shows the real `n` next to it.
5. **Limitation accepted:** after the first `shadow` start, `off` is an
   emergency mode; observations written then are not annotated later.
6. **Fewer than 18 annotations is expected** (the first case of a
   `(classifier, category)` group without an earlier decision on another
   device has no evidence). REHEARSAL gives a forecast for a stated
   order, not a production expectation.
7. **Normal path in one transaction** with the annotator in a savepoint
   (§4.1), instead of a separate transaction after the commit.

## 9. ADR amendments once this plan is accepted

`annotation_failed` on both paths with `source`; storage in
`precedent_audit` and the exact moment of `layer_started` (closes open
question 4); option `precedent_mode`; normal path in the `_persist()`
transaction via savepoint; `not_reviewable`; maturity bands as a pure
function of the evidence count (closes open question 1); limitation 5;
REHEARSAL as a forecast.
