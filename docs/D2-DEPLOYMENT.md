# D2 — STEP 7P deployment (shadow mode)

**Status:** PROCEDURE — approved conditionally 2026-10-01; conditions met
(dependency pins, D1-on-D2-schema proof). Nothing deployed yet.
**Source state:** `8570f77` (package contents); this document is committed
on top of it without touching any packaged file, and the final package on
`/share/d2` is rebuilt from that final commit.
**Production today:** D1 (`d1-deployed`).
**Gate:** FULL GATE. First production-changing operation: Phase 4.

**Tag meaning.** `d2-deployed` = STEP 7P code and schema deployed **in
shadow mode** — observation sources are measured, nothing is written to
observations, inputs or review cases. It does **not** mean 7P is active.
Activation is a separate decision after S1–S6 with its own attestation
(proposed tag `7p-active`).

---

## Scope

| Changes | Does not change |
|---|---|
| `src/` replaced by the git tree of `8570f77` (16 files) | `Dockerfile`, `run.sh` |
| `config.yaml`: `schema: false` → `options.observation_mode: shadow`, `schema: list(shadow\|active)` | add-on version `0.2.0` (so `rebuild` stays valid) |
| `requirements.txt`: unpinned → fully pinned (`aiohttp==3.14.3` + all transitive deps, Python 3.11 / linux x86_64) | the 13 D1 triggers, every existing row |
| On startup: `classification_inputs`, `classification_sweeps`, `classification_observations.input_id`, 5 triggers | `classification_observations`, `review_cases`, `assets`, `classification_inputs` — shadow writes none |
| Every sweep writes one `classification_sweeps` row | |

The 18 old `*_backup.py` files inside HAOS `src/` (in the image, never
imported) move with the old tree to `backup_d2/src_d1/`; afterwards
`/app/src` equals the git tree exactly. `assets` stays a historical STEP 1
table and is expected to stay at its current count (13).

## Pre-production evidence (local, 2026-10-01)

- **Tests:** 222/222 on a clean `git archive` of `8570f77`, in two
  environments: Python 3.11.15 with exactly the pinned requirements
  (container equivalent), and Python 3.13 with aiohttp 3.9.1. Includes the
  replay of the production Probe 7P report (160/148/18/130/20/4).
- **Real WebSocket:** `tests/test_ha_adapter_live_ws.py` runs the adapter
  against a real aiohttp server: startup + debounced event sweep,
  `get_states` > 4 MB, reconnect after close, hanging read → timeout →
  reconnect.
- **D1-on-D2-schema = PASS** (`tests/test_rollback_d1_on_d2.py`): the exact
  D1 production `storage.py` (`6bb9c184…`) and `core.py` (`c3a70399…`) on a
  D2-migrated database — startup without error or warning, D1 write path
  (`on_device_detected` → observation, review case, asset; `input_id` NULL),
  reads/analytics, D0 protection (second decision `already_resolved` on both
  paths), D1 and D2 triggers still enforce, roll-forward to D2 with every
  row and the gate state intact. Rollback A is therefore proven, not assumed.
- **Negative control rehearsal:** `d2_verify.py` on a pre-D2 database fails
  only in D2 (tested).

## Package

Built by `git -c core.autocrlf=false archive` (plain `git archive` with
`autocrlf=true` emits CRLF), every file compared to its git blob, no CR
bytes, copied to `\\homeassistant.local\share\d2`, re-verified there.

```
9cbc1ec0e24d562ae1c5f77323346560bb41c87fbae4eec86c8111f7ed16e009  config.yaml
5d2992424d1a12c1917fc0e865d7c85a18be00b11d868c6409fdf061cafa6d88  requirements.txt
ce72359a04088f50d39e6ae85c8c729a8c1aecabafaff508a49b51cc80829d0f  d2_verify.py
10bcb3f991c6fb805db0f373e2069ebbe9032a1fd070bdcc4b32a5a32457eadc  shadow_check.py
92dec3f9c24b152c50d6d5926c31d1c095a8459f4754d56cbcdc0f623a334078  SRC_SHA256SUMS
9e365163fc968c27967f2ed85bc3699bdfee0470515239287862e61877c1ba8b  (SHA256SUMS)
```
Key `src/` files (all 16 in `SRC_SHA256SUMS`):
```
29a756d04fe8f647ca9f322bfd4a30a390af9bc15f61b7dfe9405ad227a13657  storage.py
d0feedc4e71dd306b64e04c40decd953a6fe7fc926cbed3ce97de9a75a673b08  core.py
8a767304d196e39aba8c546b5d9669b53798cf9d79f8020e549f6e3bcd9e0726  main.py
032744f35f4d5998bb4d3099c4f966f24a739c78fe6099b2b26052babebb9a4a  adapters/ha.py
```

---

## Console variables (set once per shell)

```
A=/mnt/data/supervisor/apps/local/cognitive_core
S=/mnt/data/supervisor/share/d2
C=app_local_cognitive_core
```

## Phase 0 — package integrity (read-only)

```
cd $S
sha256sum SHA256SUMS
sha256sum -c SHA256SUMS
cd $S/src
sha256sum -c ../SRC_SHA256SUMS
```
Expect `9e365163…c1ba8b`, 5× OK, 16× OK. Any mismatch → stop.

## Phase 1 — baseline (read-only)

```
cd $A
sha256sum config.yaml requirements.txt
sha256sum src/storage.py src/main.py src/core.py
docker exec $C sha256sum /app/src/storage.py /app/src/main.py /app/src/core.py
docker exec $C cat /data/options.json
docker exec $C python3 -c "import aiohttp;print(aiohttp.__version__)"
```
Expect config `5d8818c9…aeaf904`, requirements `9d5a729d…124ee0d`, storage
`6bb9c184…d13bd3669`, main `d256a2d5…b32676cec`, core `c3a70399…4141fabb1`
(D1 state; every git-tracked file in HAOS `src/` was verified equal to
`d1-deployed` via Samba on 2026-10-01).

## Phase 2 — rollback point (writes only backup files)

```
mkdir $A/backup_d2
cp $A/config.yaml $A/requirements.txt $A/backup_d2/
docker exec $C python3 -c "import sqlite3;sqlite3.connect('/data/core.db').backup(sqlite3.connect('/tmp/pre_d2.db'));print('backup ok')"
docker cp $C:/tmp/pre_d2.db $A/backup_d2/pre_d2.db
sha256sum $A/backup_d2/*
docker exec $C sha256sum /tmp/pre_d2.db
```
Both `pre_d2.db` hashes equal. Record it.

## Phase 3 — negative control (old container, live DB read-only)

```
docker cp $S/d2_verify.py $C:/tmp/
docker exec $C python3 /tmp/d2_verify.py
```
Expected — **fails only in D2 elements**:
```
D0      PASS
D1      PASS      13/13 triggers, 13/13 refusals
D2      FAIL      missing trigger ×5, missing table ×2, missing column input_id
PINS    FAIL      (unpinned D1 image; lists each differing version — recorded)
DATA    PASS
SHADOW  PASS      classification_inputs: table absent
RESULT: FAIL
```
D0 or D1 not PASS → stop: the problem predates D2.

## ⚠ Phase 4 — deploy source (FIRST PRODUCTION-CHANGING STEP)

```
cd $A
mv src backup_d2/src_d1
cp -r $S/src src
cp $S/config.yaml $S/requirements.txt .
cd $A/src
sha256sum -c $S/SRC_SHA256SUMS
find . -type f | wc -l
cd $A
sha256sum config.yaml requirements.txt
```
Nothing deleted (old tree moved). Expect 16× OK, `16`, config `9cbc1ec0…`,
requirements `5d299242…`.

## Phase 5 — reload config + rebuild

```
ha store reload
ha apps rebuild local_cognitive_core --force
ha apps info local_cognitive_core
```
`store reload` makes the Supervisor read the new `config.yaml` (fallback:
Settings → Apps → App store → ⋮ → Check for updates). `apps info` must show
`observation_mode: shadow`. Supervisor rejects the schema or the build
fails → rollback A.

## Phase 6 — running container == `8570f77`

```
docker exec $C sh -c "cd /app/src && sha256sum -c /share/d2/SRC_SHA256SUMS"
docker exec $C sh -c "find /app/src -type f -not -path '*__pycache__*' | wc -l"
docker exec $C cat /data/options.json
docker exec $C python3 -c "import aiohttp;print(aiohttp.__version__)"
```
Expect 16× OK, `16`, `{"observation_mode": "shadow"}` (a missing key still
means shadow — recorded, not a stop), `3.14.3`.

## Phase 7 — startup log

```
docker logs $C 2>&1 | head -60
```
In order: `[OPTIONS] observation_mode=shadow`, `[STORAGE] STEP 7P schema
verified (5 triggers)`, `backfill_review_cases: 0 created`, `Subscribed to
device_registry_updated`, `Subscribed to entity_registry_updated`, `[SWEEP]
swp_… mode=shadow source=startup discovered=… error=0`.

## Phase 8 — `d2_verify.py` (cumulative)

```
docker cp $A/backup_d2/pre_d2.db $C:/tmp/
docker cp $S/d2_verify.py $C:/tmp/
docker exec $C python3 /tmp/d2_verify.py
```
(`/tmp` is cleared by the rebuild.) Required, every section:
```
D0      PASS   every resolved case re-decided on a copy with the deployed code -> already_resolved
D1      PASS   13/13 triggers, 13/13 exact refusals, review-only UPDATE allowed
D2      PASS   5/5 triggers, 5/5 exact refusals, no unexpected trigger
PINS    PASS   every pinned package installed at the pinned version
DATA    PASS   every pre-D2 row identical; input_id NULL on all pre-D2 observations
SHADOW  PASS   observations, review_cases, assets equal to the snapshot; inputs 0; sweeps all shadow
RESULT: PASS
```

## Phase 9 — shadow evidence (first day)

```
docker exec $C python3 /share/d2/shadow_check.py
```
Expected now: S1 MET, S2 MET, S3 MET (or CHECK, differences explained by
registry changes since Probe 7P), **S4 NOT MET** (needs a daily sweep ≥ 20 h
after startup), S5 MET, S6 MET → `RESULT: NOT READY`. That is the expected
D2 end state.

## Phase 10 — attestation

This document becomes the attestation with the observed values, is
committed, and only then tagged `d2-deployed` (= deployed in shadow).

---

## After D2 — activation (lightweight FULL GATE, separate decision)

After ≥ 1 daily shadow sweep: `shadow_check.py` again. All S1–S6 MET →
explicit **GO** → `observation_mode: active` in the add-on UI → restart →
own attestation (proposed tag `7p-active`). Not part of D2.

---

## Rollback

**A — code/config/requirements (any phase ≥ 4)** — proven by
`tests/test_rollback_d1_on_d2.py`:
```
cd $A
mv src backup_d2/src_d2_failed
mv backup_d2/src_d1 src
cp backup_d2/config.yaml backup_d2/requirements.txt .
ha store reload
ha apps rebuild local_cognitive_core --force
```
Then the Phase 1 hashes must reappear. The D2 tables, `input_id` column and
5 triggers stay in `/data/core.db`; the D1 code runs on them unchanged
(tested with the exact D1 production files).

**B — database (break-glass only):** restore `backup_d2/pre_d2.db` with the
add-on stopped. Loses every write after Phase 2; only if the database
itself is damaged.
