# D3 — STEP 7a deployment (precedent layer off)

**Status:** Deployment verified — PASS. Precedent layer: **off, never started**.
**Date:** 2026-10-03
**Deployed source state:** `2d8dfb1` (`src/` = 21 files, `config.yaml`)
**Verification tool:** `scripts/d3_verify.py` (cumulative D0 + D1 + D2 + D3 +
PINS + DATA + RESOLVER + LAYER + REHEARSAL), with `scripts/d2_verify.py` next
to it, both from the same package
**Predecessors:** `docs/7P-ACTIVATION.md`, `docs/STEP7_ARCHITECTURE.md`,
`docs/STEP7A_IMPLEMENTATION_PLAN.md`
**Gate:** FULL GATE. First production-changing operation: Phase 4.

**Tag meaning.** `d3-deployed` = STEP 7a code and schema deployed with
`precedent_mode: off`. The three precedent tables exist and are empty; the
layer has **not** started (no `layer_started` row), no annotation exists and
the resolver behaves exactly as under STEP 7P. It does **not** mean 7a is
running. Enabling `shadow` is a separate FULL GATE with its own attestation
(proposed tag `7a-shadow`).

---

## Scope

| Changes | Does not change |
|---|---|
| `src/` replaced by the git tree of `2d8dfb1` (21 files; new: `precedent_store.py`, `precedent_annotator.py`, `precedent_report.py`, `learning/`) | `Dockerfile`, `run.sh`, `requirements.txt` (`5d299242…`, identical) |
| `config.yaml`: option `precedent_mode: "off"`, schema `list(off\|shadow)` | add-on version `0.2.0`; option `observation_mode: active` (kept by the Supervisor) |
| On startup: `precedent_annotations`, `precedent_annotation_evidence`, `precedent_audit`, index `idx_precedent_layer_started`, 6 append-only triggers (24 in total) | every existing row; the 18 D1 + 7P triggers; the 18 pending and 2 resolved review cases |
| Write path: one transaction per classified device (`record_classification`); one canonical resolver, legacy methods delegate (`not_reviewable`) | what STEP 7P writes — proven equal to `8570f77` by `tests/test_7p_equivalence.py` |

## Pre-production evidence (local, 2026-10-03)

- **Tests:** 397 passed, 1 skipped (247 before 7a). New modules covered
  96–100 % (stdlib `trace`).
- **Equivalence gate:** `tests/test_7p_equivalence.py` 3/3 — with
  `precedent_mode=off` the same inputs, observations, review cases,
  fingerprint gating and sweep results as `8570f77`; `shadow` changes
  nothing STEP 7P writes.
- **Fault injection:** a failing review case, a failing annotator and a
  failing `annotation_failed` write roll back nothing else; a fatal
  transaction failure leaves the case pending and a retry resolves it
  exactly once.
- **Preflight on a snapshot of the live database** (`pre_d3.db`
  `7905a0d4543738d179bcd2a257b01f78ff3169bbb180862257f2925b3996172c`,
  backup API, 20 cases / 18 pending): migrated copy, all sections PASS
  except PINS (Windows interpreter, not counted); REHEARSAL 16 / 2 / 0.
- **Negative control, local:** `8570f77` code on `pre_d3.db` — D0, D1, D2,
  DATA pass; D3, RESOLVER, LAYER, REHEARSAL fail.
- **Rollback proof:** `8570f77` code on the 7a-migrated copy starts, lists
  18 pending cases, resolves one on the copy, leaves the precedent tables
  empty.
- **LAYER duplicate rule:** the key is the logical case
  `(device_id, classifier_name, hypothesis_category)`; several devices
  decided for one `(classifier, category)` are the class pattern's evidence.

## Package

Built with `git -c core.autocrlf=false show 2d8dfb1:<path>`, every file
compared to its git blob, no CR bytes, copied to
`\\homeassistant.local\share\d3`, re-verified there.

```
ea2a4123310fac4949a8278b6d645a112c514cbd9f851a83c5d1978a2ac8c66d  config.yaml
5d2992424d1a12c1917fc0e865d7c85a18be00b11d868c6409fdf061cafa6d88  requirements.txt
374241445cb842920e26631847242b84e7686799ebf4f7a851f1898f50fd0dd4  d3_verify.py
ce72359a04088f50d39e6ae85c8c729a8c1aecabafaff508a49b51cc80829d0f  d2_verify.py
6faa0ab2f9844b0ab4c12f12df89727bee6b2e4355755e1b8ccece1591ce7471  SRC_SHA256SUMS
8e9ee69b3c39e26c3715b71dd46f0c7e4b1ca586d3b4907e96c3f99982f44561  (SHA256SUMS)
```
Key `src/` files (all 21 in `SRC_SHA256SUMS`):
```
9ac0c4edd80d13d951f4d0b7df78fabb618ee2688751795ac705d5132ed7de47  storage.py
0a97b2d670ecd761ee814a62fcc5f52726ffe6ef175fc424b9726963b62ea5f8  core.py
a9b43afe8f500118bacf28fd53874c109a0236fc27eeeec50e7f951812ca58b4  precedent_store.py
988ca57958e460883be65bf019333543cf175001c9be2d150f17af970c797126  precedent_annotator.py
536f7af52345abb4a769900e7145522fbc4f0f04f780598a0894d8c3be693bad  learning/precedent.py
```
Read-only helpers on the share, added to the repository afterwards
byte-identical: `snap.py` = `scripts/d3_snapshot.py` (`78523568…c8ed35`),
`state.py` = `scripts/d3_state.py` (`23d7595b…7a1cac`).

## Console variables

```
A=/mnt/data/supervisor/apps/local/cognitive_core
S=/mnt/data/supervisor/share/d3
C=app_local_cognitive_core
```

---

## Observed results

### Phase 0 — package integrity: PASS
`SHA256SUMS` = `8e9ee69b…82f44561`; 5× OK; 21× OK in `src/`.

### Phase 1 — baseline (7P active): PASS
`/data/options.json` = `{"observation_mode": "active"}` (no
`precedent_mode`).

### Phase 2 — rollback point: PASS
`$A/backup_d3/`: `pre_d3_final.db`
`eeb4c9344076ee6a0c59c6b07b3c8e203ead26b63febc8a115b941c6ad81a50e`
(145 inputs, 31 observations, 42 sweeps, 18 pending / 2 resolved,
`integrity_check` ok; same hash on the console and over Samba; logical
content identical to the preflight snapshot `pre_d3.db`, which is kept),
`pre_d3_config.yaml` (`9cbc1ec0…`), and after Phase 4 `src_7p/` (16 files,
`storage.py` `29a756d0…`). `find $A -name 'config.*'` → only `config.yaml`.

### Phase 3 — negative control (old container): as expected
D0, D1, D2, PINS, DATA PASS. **D3 FAIL** (6 triggers, 3 tables, the index
missing); **RESOLVER FAIL** (the 7P legacy methods label a non-current
observation); **LAYER FAIL** (tables absent); **REHEARSAL FAIL**
(`Storage` has no `precedent_mode`). Not failures of D0–D2.

### Phase 4 — source deployed: PASS
`src` moved to `backup_d3/src_7p`, package copied; 21× OK on the host (and
over Samba, 21 files), `config.yaml` `ea2a4123…2ac8c66d`, one `config.*`.

### Phase 5 — rebuild: PASS
`ha store reload`, `ha apps rebuild local_cognitive_core --force`, both
completed successfully. `ha apps start` was issued afterwards and also
completed; the log shows a single startup.

### Phase 6 — running container == `2d8dfb1`: PASS
21/21 hashes OK inside the container. `/data/options.json` =
`{"observation_mode": "active", "precedent_mode": "off"}`. Log:
`precedent_mode=off (source: /data/options.json)`,
`STEP 7a schema verified (6 triggers)`, build `2026-10-03-step7a`, no
`[PRECEDENT] layer started` line. First sweep:

```
swp_3e212d69f92a active startup
discovered=159 no_entities=14 missing_metadata=0 unchanged=145
classified=0 no_match=0 error=0
```

### Phase 7 — `d3_verify.py` in the container: PASS
```
D0        PASS
D1        PASS
D2        PASS
D3        PASS
PINS      PASS
DATA      PASS
RESOLVER  PASS   one label and one resolve statement, both in storage.py;
                 36/36 legacy calls on non-current observations refused as
                 not_reviewable; delegation on current evidence: yes
LAYER     PASS   precedent_mode off; layer_started rows 0 (never started);
                 annotations 0; annotation_failed 0 / 0; logical keys with
                 more than one labelled observation: 0
REHEARSAL PASS   forecast only
RESULT: PASS
```
The console capture (`tail -45`) shows the result table and the RESOLVER,
LAYER and REHEARSAL details; the detail lines of D0–DATA were not captured
in this run.

**REHEARSAL (forecast, not a gate invariant)** — 18 pending cases, in the
order the `review` CLI lists them, hypothetical decision `approved`:
annotations written for **16**, without earlier evidence **2**
(`case_d5ecd1b0` motion_sensor, `case_1d4e1ff4` energy_meter),
`annotation_failed` **0**. Groups: 9 energy_meter, 5 environmental_sensor,
4 motion_sensor. Identical to the preflight. A different real review order
changes which case of a group comes first.

### Live state after D3 (`state.py`, read-only)
```
precedent_annotations 0
precedent_annotation_evidence 0
precedent_audit 0
classification_inputs 145
classification_observations 31
classification_sweeps 43
review_cases [('pending', 18), ('resolved', 2)]
labelled observations 2
triggers 24
last sweep ('swp_3e212d69f92a', 'active', 'startup', 145, 0, 0, 0)
```
D3 with the layer off created no annotation, no evidence row, no
`layer_started` and no `annotation_failed`; the 18 pending cases are
untouched.

---

## Rollback

`mv $A/src $A/backup_d3/src_7a_failed`, `mv $A/backup_d3/src_7p $A/src`,
`cp $A/backup_d3/pre_d3_config.yaml $A/config.yaml`,
`find $A -name 'config.*'` (only `config.yaml`), `ha store reload`,
`ha apps rebuild local_cognitive_core --force`. The three empty tables and
six triggers stay; the 7P code runs on them (proven on a copy).
`d2_verify.py` and `active_verify.py` report the six precedent triggers as
unexpected after D3, by design.

## Not done by D3

`precedent_mode` stays `off`. No case was reviewed. Enabling the layer
(`7a-shadow`) needs its own GO: backup, UI option, exactly one
`layer_started` before the first sweep of that start, 0 annotations,
18 pending untouched.
