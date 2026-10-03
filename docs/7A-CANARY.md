# STEP 7a — canary review (first real decisions under the precedent layer)

**Status:** Mechanism verified — PASS. **Scope deviation:** three cases were
decided; the GO covered two.
**Date:** 2026-10-03
**Running source:** `2d8dfb1`, `precedent_mode: shadow` (`docs/7A-ENABLE.md`)
**Verification tools:** `scripts/review_check.py` (`364b5ad`),
`scripts/d3_verify.py`, package `/share/rv`
**Gate:** FULL GATE — human decisions and annotations are append-only.
**Tag:** none (not agreed for the canary).

---

## Package `/share/rv` (git objects of `364b5ad`)

```
36e91602891e90a91e818080c2062c7c0633bdd8bbf7eccd146e2b2dd5a62854  review_check.py
02e2f229585f8bf2ffebde2dd4c9047f123c8e0e68ccc81fa756d008e03d84c9  snap.py
374241445cb842920e26631847242b84e7686799ebf4f7a851f1898f50fd0dd4  d3_verify.py
ce72359a04088f50d39e6ae85c8c729a8c1aecabafaff508a49b51cc80829d0f  d2_verify.py
23d7595b526205c051e4f7151e636817a536f67208548ad907a7ae19d27a1cac  state.py
f268598eaa531b5b76a0d8afe09c707f8c3d975e1388b8f81584dfcc48f84d73  (SHA256SUMS)
```
Local evidence: 417 tests passed, 1 skipped; the two-case canary rehearsed
on a copy of `pre_7a.db`.

## Before

Backup `$A/backup_7a/pre_canary.db`
`af172d36168960d290167a0317ccf0002763387491f4ca56d4c25029effe2403`
(same hash on the console and over Samba; `integrity_check` ok): 145 inputs,
31 observations, 44 sweeps, `precedent_audit` 1, annotations 0, evidence 0,
18 pending / 2 resolved, 24 triggers.

## The review

`docker exec -it … python3 -m src.main review`, cases in the order the CLI
lists them (the REHEARSAL order). Each decision was the reviewer's own
judgment of the observation; the review queue shows no annotation.

| # | Case | Hypothesis | Decision | `decided_at` (UTC) |
|---|---|---|---|---|
| 1 | `case_d5ecd1b0` | motion_sensor | approved | 21:40:52.966139 |
| 2 | `case_217c1dc1` | motion_sensor | approved | 21:41:25.644163 |
| 3 | `case_8d9428f6` | environmental_sensor | approved | 21:41:58.384831 |

**Deviation.** The GO was for cases 1 and 2 only, then stop. The reviewer
also decided case 3 (deliberately, not a mistyped key) and interrupted at
case 4. The decision is a genuine blind human judgment and every check
below passes for it; it cannot be undone (decisions are immutable) and
does not need to be. 15 cases remain pending instead of 16.

## `review_check.py`: PASS

```
LAYER      PASS   layer_started rows 1 (cut-off 2026-10-03T21:09:21.564070Z);
                  annotation_failed 0 (0 since the snapshot)
DECISIONS  PASS   3 cases resolved since the snapshot, all pending in it,
                  all decided after the cut-off, observation = case decision
EVIDENCE   PASS
UNTOUCHED  PASS   snapshot 18 pending / 2 resolved -> live 15 / 5;
                  +0 inputs, +0 observations, +0 review cases
RESULT: PASS
```

The chain, as printed:

```
#1 case_d5ecd1b0: no annotation; earlier decisions on other devices: 0
#2 case_217c1dc1: pan_c7fa52a7afb8 class_pattern bootstrap_pending -> approved,
   evidence 1/1 insufficient n=1, created_at 21:41:25.642208
     evidence: case_d5ecd1b0 obs obs_91cdb983ab25 approved at 21:40:52.966139
#3 case_8d9428f6: pan_b02910e4f781 class_pattern bootstrap_pending -> approved,
   evidence 2/2 insufficient n=2, created_at 21:41:58.382826
     evidence: case_8dc45785 obs obs_38db67ece910 approved at 2026-08-07T20:06:44.794391
     evidence: case_a35cab51 obs obs_b22058a8cfbf approved at 2026-08-08T20:19:43.895131
```

- **Decision 1 → evidence → blind decision 2.** Case 1 had no earlier
  evidence and got no annotation (not a failure). Case 2's annotation was
  written 1.96 ms before its decision; its only evidence is decision 1, on
  another device; decision 2 is not in its own evidence.
- **Case 3** used the two decisions that predate the layer (August), on two
  other devices; written 2.0 ms before its decision. Cases 2 and 3 belong
  to the same device with different hypotheses — separate logical keys.
- Every suggestion happened to match the decision; with n = 1 and n = 2
  (`insufficient`) that says nothing about accuracy.

## `d3_verify.py --pre-db /tmp/pre_review.db`: PASS

D0, D1, D2, D3, PINS, DATA, RESOLVER, LAYER, REHEARSAL all PASS. LAYER:
annotations 2, both `class_pattern` / `bootstrap_pending`, fully consistent
2/2 (evidence rows, digest, order, cut-off); `annotation_failed` 0 / 0; no
logical key with two labelled observations; observations since the cut-off
0. REHEARSAL (forecast) for the 15 remaining: 14 annotated, 1 without
earlier evidence (`case_1d4e1ff4`, the first energy_meter), 0 failed.

## Live state (`state.py`)

```
precedent_annotations 2
precedent_annotation_evidence 3
precedent_audit 1
classification_inputs 145
classification_observations 31
classification_sweeps 44
review_cases [('pending', 15), ('resolved', 5)]
labelled observations 5
triggers 24
last sweep ('swp_0ce49f658e50', 'active', 'startup', 145, 0, 0, 0)
```

## Not done

The remaining 15 cases are not reviewed; that needs its own GO.
`precedent-report` has not been run yet.
