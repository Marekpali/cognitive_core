# STEP 7a — precedent layer enabled (shadow)

**Status:** Enabled — PASS. `precedent_mode: shadow`; layer started once;
no annotation exists; no case reviewed.
**Date:** 2026-10-03
**Running source:** `2d8dfb1` (unchanged since D3, `docs/D3-DEPLOYMENT.md`)
**Verification tools:** `scripts/layer_start_check.py` (`0f8e8e2`) and the
cumulative `scripts/d3_verify.py`, package `/share/7a`
**Gate:** FULL GATE. The only production change: the add-on option
`precedent_mode` `off` → `shadow` in the Home Assistant UI.

**Tag meaning.** `7a-shadow` = the precedent layer runs in shadow: new
observations are annotated when written, and a case that was pending before
the cut-off is annotated inside the resolver immediately before its first
human decision. Annotations have no authority and are not shown in the
review queue. The tag is **not** a go-ahead for reviewing the 18 pending
cases; that controlled bootstrap/review needs its own GO.

---

## Package `/share/7a` (git objects of `0f8e8e2`)

```
e0117874198399af8e621492ca672fe2e6eaf8bf735ce102811ae450d6e9b526  layer_start_check.py
02e2f229585f8bf2ffebde2dd4c9047f123c8e0e68ccc81fa756d008e03d84c9  snap.py   (scripts/d3_snapshot.py)
374241445cb842920e26631847242b84e7686799ebf4f7a851f1898f50fd0dd4  d3_verify.py
ce72359a04088f50d39e6ae85c8c729a8c1aecabafaff508a49b51cc80829d0f  d2_verify.py
5d2992424d1a12c1917fc0e865d7c85a18be00b11d868c6409fdf061cafa6d88  requirements.txt
4c870e729faad5c741292ad358e46df9b00b34229e019dcfb0a2c88d4c8db597  (SHA256SUMS)
```
`d3_verify.py` and `d2_verify.py` are the files used in D3. Local evidence
before the gate: 407 tests passed, 1 skipped; on a copy of `pre_d3_final.db`
with the layer started, `d3_verify` passes in the state "started, 0
annotations".

## Observed results

### 1 — package and backup: PASS
`SHA256SUMS` `4c870e72…`, 5× OK. `$A/backup_7a/pre_7a.db`
`941d5f27628572a45b28b35f90f66ec5ec49e657c25265d21fa944bb60622028`
(backup API; same hash on the console and over Samba; `integrity_check` ok).

### 2 — baseline before the change: PASS
145 inputs, 31 observations, 43 sweeps; `precedent_annotations` 0,
`precedent_annotation_evidence` 0, `precedent_audit` 0; 18 pending /
2 resolved; 24 triggers; options `observation_mode: active`,
`precedent_mode: off`; last sweep `swp_3e212d69f92a` (the D3 startup).

### 3–4 — option changed in the UI, normal restart
Only `precedent_mode` was changed; saving restarted the app by itself. No
manual restart, no review command, no decision.

### 5–6 — `layer_start_check.py`: PASS
```
OPTION  PASS   precedent_mode shadow, observation_mode active
CUTOFF  PASS   exactly one layer_started, source NULL, absent from the snapshot
ORDER   PASS   cut-off 2026-10-03T21:09:21.564070Z; the only sweep since the
               snapshot, swp_0ce49f658e50 (active, startup), started at
               2026-10-03T21:09:21.842389Z - 0.278 s AFTER the cut-off
QUIET   PASS   annotations 0, evidence 0, annotation_failed 0; review cases
               18 pending / 2 resolved as in the snapshot; +0 inputs,
               +0 observations, +0 review cases
RESULT: PASS
```
The cut-off was written before the first sweep of the first shadow process.

### 7 — `d3_verify.py --pre-db /tmp/pre_7a.db` in the container: PASS
```
D0 D1 D2 D3 PINS DATA RESOLVER LAYER REHEARSAL   all PASS
LAYER      option shadow; layer_started rows 1 (cut-off 21:09:21.564070Z);
           annotations 0; annotation_failed 0 / 0; logical keys with more
           than one labelled observation 0; observations since the
           cut-off 0
REHEARSAL  18 pending; forecast 16 annotated, 2 without earlier evidence,
           annotation_failed 0 (forecast only, unchanged since D3)
RESULT: PASS
```
The console capture shows the result table and the RESOLVER, LAYER and
REHEARSAL details; the detail lines of D0–DATA and the option/log output
were not captured.

### Live state (`state.py`, read-only)
```
precedent_annotations 0
precedent_annotation_evidence 0
precedent_audit 1
classification_inputs 145
classification_observations 31
classification_sweeps 44
review_cases [('pending', 18), ('resolved', 2)]
labelled observations 2
triggers 24
last sweep ('swp_0ce49f658e50', 'active', 'startup', 145, 0, 0, 0)
```
One audit row (the cut-off), nothing else: no annotation before any review,
no `annotation_failed`, the 2 resolved cases unchanged, no STEP 7P write
(startup sweep `unchanged=145`).

---

## Rollback

Option `precedent_mode` back to `off` in the UI — operational only. The
`layer_started` row and any later annotation or audit row stay
(append-only); observations written while `off` are never annotated
afterwards.

## Not done

No review case was resolved. The 18 pending cases wait for a separate GO.
