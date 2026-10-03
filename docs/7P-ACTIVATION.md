# STEP 7P activation — shadow → active

**Status:** PROCEDURE — prepared 2026-10-03. Nothing activated. Production
runs `8570f77` in `observation_mode: shadow` (`d2-deployed`).
**Gate:** FULL GATE — lightweight, single decision. No code, no rebuild:
one add-on option and a restart. First production-changing operation:
Phase A4, only after an explicit **GO**.
**Predecessors:** `docs/D2-DEPLOYMENT.md`, `docs/STEP7_OBSERVATION_SOURCES.md`
(§8, §10.2 incl. the S4 amendment).
**Verification tool:** `scripts/active_verify.py` (new; reuses the D0, D1,
D2, PINS and DATA sections of the unchanged `scripts/d2_verify.py`).
**Tag after attestation:** `7p-active`.

---

## Precondition — shadow criteria (met 2026-10-03)

`shadow_check.py` @ `b82c39d` on production, 27 shadow sweeps:

```
S1 MET  S2 MET  S3 MET (startup = effective = probe = 18/20/4)
S4 MET  longest stable window 20.5 h, 14 sweeps, 146 devices identical;
        whole period: changed 11 (more than once 0), added 2, removed 3;
        gate inconsistencies: none
S5 MET  0/145   S6 MET  changes since pre-D2: none
RESULT: ALL MET - ready for GO decision
```

## What activation does

The first active sweep has no `classification_inputs` to compare against,
so it evaluates **every device with entities**:

| Written | How many |
|---|---|
| `classification_inputs` | one per evaluated device, `no_match` included |
| `classification_observations` | one per match, each with `input_id` |
| `review_cases` | a new `pending` case per newly observed hypothesis; an existing case only advances `last_observation_id` |
| `classification_sweeps` | one row, `mode = active` |

Every later sweep is gated against the latest input per device: unchanged
fingerprint → nothing written.

Not changed: every existing observation; status, decision,
`corrected_category`, `decided_at` of every existing review case (a resolved
case never reopens — `upsert_review_case` does not touch those fields, and
D0/D1 protect them); `assets`; code; image.

Visible effect: the pending-reviews sensor rises by the number of new
pending cases.

**These rows are permanent.** `classification_inputs` and
`classification_sweeps` are append-only; observations are immutable history.
There is no undo of an activation, only a return to shadow (see Rollback).

## `active_verify.py`

Read-only on the live database (`mode=ro`); every refusal test that needs
UPDATE/INSERT/DELETE runs on backup-API copies in `/tmp`, exactly as in
`d2_verify.py`. Prints ids, counts and hashes only.

| Section | Checks |
|---|---|
| D0, D1, D2, PINS | as in `d2_verify.py` |
| DATA | every row of `pre_active.db` still present and identical; `review_cases` may differ only in `last_observation_id`/`updated_at`, and only towards new evidence; pre-activation observations keep `input_id` NULL |
| ACTIVE | **nothing hard-coded** — expectations are derived from the stored active sweep rows. Gate replayed from `classification_inputs`; each evaluated device has exactly one matching input in its sweep; each input has exactly its matched observations, linked by `input_id`, undecided; each observed hypothesis has a review case pointing at its newest observation; new cases are plain `pending`; **cases resolved in the snapshot are still resolved with identical decision fields**; no asset written; no shadow sweep after the first active one |
| REPEAT | a later active sweep exists and the replayed gate holds for it (same fingerprint → `unchanged` → nothing written; a device whose fingerprint really changed is re-evaluated consistently). `PENDING` until a second active sweep happened |

`RESULT`: `PASS` (exit 0), `INCOMPLETE - second active sweep pending`
(exit 2), `FAIL` (exit 1).

Tests: `tests/test_active_verify.py` (20), including the negative control,
a later sweep with a real registry change, eight ways of breaking the
chain, and replay of inconsistent gate decisions.

## Rehearsal on a copy of the real database (local, 2026-10-03)

Copy of production `pre_d2.db` (`6e41d3f9…abbf36e`), production registry
payloads from Probe 7P, repository code:

```
before activation   observations 11  review_cases 2 (resolved 2)  inputs 0  assets 13
negative control    D0 D1 D2 PINS DATA PASS; ACTIVE FAIL "no active sweep since the snapshot"
first active sweep  discovered 160, evaluated 148 (classified 18), unchanged 0, error 0
                    inputs +148, observations +20 (expected from inputs: 20)
                    review cases: 18 new pending, 2 existing advanced to new evidence
                    resolved in snapshot: 2; still resolved, identical decision fields: 2
                    -> ACTIVE PASS, REPEAT PENDING, RESULT INCOMPLETE
second active sweep discovered 160, evaluated 0, unchanged 148, inputs +0, observations +0
                    -> RESULT PASS
return to shadow    shadow sweep: 148 unchanged, nothing written
active again        active sweep: 148 unchanged, nothing written
```

The two decided hypotheses are among the 20 matches: their cases stay
resolved and only point at the new evidence. Production numbers will differ
slightly (159 devices, 145 with enabled entities); the verifier derives
them from the sweep, it does not assume them.

## Package (`/share/7pa`)

Built from git objects with `core.autocrlf=false`, LF, compared to the blobs.

```
64505be224e0f7bbdcc8a7aa1b14f2771f96f2cbc769f1d9cb438e693404a9e8  active_verify.py
ce72359a04088f50d39e6ae85c8c729a8c1aecabafaff508a49b51cc80829d0f  d2_verify.py   (identical to the D2 package)
```

---

## Console variables

```
A=/mnt/data/supervisor/apps/local/cognitive_core
S=/mnt/data/supervisor/share/7pa
C=app_local_cognitive_core
```

## Phase A0 — package integrity (read-only)

```
cd $S
sha256sum -c SHA256SUMS
```
2× OK.

## Phase A1 — production is still the verified D2 state (read-only)

```
docker exec $C sh -c "cd /app/src && sha256sum -c /share/d2/SRC_SHA256SUMS"
docker exec $C cat /data/options.json
find $A -name 'config.*'
```
16× OK, `{"observation_mode": "shadow"}`, only `…/cognitive_core/config.yaml`.

## Phase A2 — rollback point and baseline (writes only backup files)

```
docker exec $C python3 -c "import sqlite3;sqlite3.connect('/data/core.db').backup(sqlite3.connect('/tmp/pre_active.db'));print('backup ok')"
docker cp $C:/tmp/pre_active.db $A/backup_d2/pre_active.db
sha256sum $A/backup_d2/pre_active.db
docker exec $C sha256sum /tmp/pre_active.db
```
Both hashes equal — recorded in the attestation.

## Phase A3 — negative control and exact baseline counts (read-only)

```
docker exec $C python3 /share/7pa/active_verify.py
```
Expected: D0, D1, D2, PINS, DATA `PASS`; `ACTIVE FAIL` with the single
reason `no active sweep since the snapshot`; `REPEAT PENDING`. The
`[ACTIVE]` block prints the baseline counts (observations, review cases,
inputs, sweeps, assets) — these are the "before" numbers of the
attestation. Anything else failing → stop; the problem predates activation.

## ⚠ Phase A4 — activation (FIRST PRODUCTION-CHANGING STEP, needs GO)

Home Assistant UI: **Settings → Apps → Cognitive Core → Configuration →
`observation_mode`: `active` → Save**, then restart the app (the UI offers
it; otherwise `ha apps restart local_cognitive_core`).

No rebuild. Do **not** decide any review case until Phase A7 is finished —
the verifier requires the new observations and cases to be undecided.

## Phase A5 — startup log (read-only)

```
docker exec $C cat /data/options.json
docker logs $C 2>&1 | grep -E "OPTIONS|SWEEP|Traceback" | tail -8
```
`{"observation_mode": "active"}`; `[OPTIONS] observation_mode=active`;
`[SWEEP] swp_… mode=active source=startup discovered=… unchanged=0 …
error=0`. A `Traceback`, `error` ≠ 0 or `mode=shadow` → stop, see Rollback.

## Phase A6 — `active_verify.py` after the first active sweep

```
docker cp $A/backup_d2/pre_active.db $C:/tmp/
docker exec $C python3 /share/7pa/active_verify.py
```
(`/tmp` is cleared by the restart.) Required:
```
D0 PASS  D1 PASS  D2 PASS  PINS PASS  DATA PASS  ACTIVE PASS  REPEAT PENDING
RESULT: INCOMPLETE - second active sweep pending
```
with `new observations: N; expected from new inputs: N`, inputs written =
evaluated, and `resolved in snapshot: 2; still resolved with identical
decision fields: 2`.

## Phase A7 — second sweep, not forced

Registry events arrive on this instance every few hours (27 sweeps in
60 h). After the next one — no action is taken to cause it — run the same
command again:
```
docker exec $C python3 /share/7pa/active_verify.py
```
Required: `REPEAT PASS`, `RESULT: PASS`. Typically
`later active sweeps: 1; with nothing evaluated or written: 1`; if the
registry really changed in between, the affected devices are re-evaluated
and the replay still has to hold. If the container restarted in between,
repeat the `docker cp` of Phase A6 first.

## Phase A8 — attestation

This document becomes the attestation with the observed values, is
committed, and only then tagged `7p-active`.

---

## Rollback

**Operational rollback to shadow** (any time after A4): set
`observation_mode: shadow` in the same UI, restart. Sweeps go back to
writing only `classification_sweeps` rows. This is **not** a database
rollback: inputs, observations and review cases written while active
remain, as valid immutable history, and the pending cases stay visible.
Returning to active later evaluates only devices whose fingerprint differs
from their latest input (rehearsed: nothing written).

**Code rollback** (to D1): unchanged, `docs/D2-DEPLOYMENT.md` Rollback A.
The D1 code runs on the D2 schema; rows written while active stay.

**Database restore — break-glass only:** `backup_d2/pre_active.db` with the
add-on stopped. Loses every write after Phase A2, including human decisions
made since. Only for a damaged database, never as a way to undo activation.

## Known edge case

A write failure on the sweep row itself after active writes leaves inputs
whose `sweep_id` has no sweep row (ADR §8); the adapter retries the sweep,
which then reports those devices `unchanged`. `active_verify.py` reports it
as `input of unknown sweep` → stop and analyse, do not repair by hand.
