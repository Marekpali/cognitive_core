# D2 — STEP 7P deployment (shadow mode)

**Status:** Deployment verified — PASS (shadow). 7P readiness: NOT READY (S4
pending by design).
**Date:** 2026-10-01
**Deployed source state:** `8570f77` (package contents; `f848ebe` adds only
the procedure document, packaged files identical)
**Verification tools:** `scripts/d2_verify.py` (cumulative D0 + D1 + D2 +
PINS + DATA + SHADOW), `scripts/shadow_check.py` (S1–S6), both from the same
package
**Predecessors:** `docs/D1-DEPLOYMENT.md`, `docs/STEP7_OBSERVATION_SOURCES.md`
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

The 17 old `*_backup.py` files inside HAOS `src/` (in the image, never
imported) moved with the old tree to `backup_d2/src_d1/` (28 files,
verified via Samba identical to the pre-D2 tree); `/app/src` now equals the
git tree exactly. `assets` stays a historical STEP 1 table (13 rows,
unchanged).

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
  D2-migrated database — startup, D1 write path, reads, D0 protection, D1
  and D2 triggers, roll-forward. Rollback A is proven, not assumed.
- **Rehearsal on a copy of the real `pre_d2.db`:** negative control as
  expected; migration + shadow sweep; full `d2_verify` PASS; D1 code on the
  migrated copy PASS.

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

## Console variables

```
A=/mnt/data/supervisor/apps/local/cognitive_core
S=/mnt/data/supervisor/share/d2
C=app_local_cognitive_core
```

---

## Observed results

### Phase 0 — package integrity: PASS
`SHA256SUMS` = `9e365163…c1ba8b`; 5× OK; 16× OK in `src/`.

### Phase 1 — baseline (D1 state): PASS
config `5d8818c9…aeaf904`, requirements `9d5a729d…124ee0d`; `storage.py`,
`main.py`, `core.py` identical on host and in the container (D1 hashes);
`/data/options.json` = `{}`; aiohttp `3.14.3`; container `bd21911e2117`, up
37 h.

### Phase 2 — rollback point: PASS
`$A/backup_d2/`: `pre_d2.db`
`6e41d3f9b3097d1ddd12b901ee7fb429f4f4409c5e15b6ceb32055b05abbf36e`
(111 kB, `integrity_check` ok, same hash in the container and on the host),
`pre_d2_config.yaml` (originally copied as `config.yaml`, renamed during the
incident below), `requirements.txt`, `options_pre_d2.json` (`44136fa3…`).

### Phase 3 — negative control (old container): as expected
D0 PASS, D1 PASS (13/13, 13/13); **D2 FAIL** only for the 8 missing 7P
elements (5 triggers, 2 tables, `input_id`); PINS 16/17 (old image had
`charset-normalizer` 3.5.1, pin is 3.5.2 — recorded; the rebuild installs
the pin); DATA PASS; SHADOW PASS.

### Phase 4 — source deployed: PASS
`src` moved to `backup_d2/src_d1`, package copied; 16× OK, 16 files,
config `9cbc1ec0…`, requirements `5d299242…`.

### Phase 5 — rebuild: PASS after the incident below
After the fix: `ha store reload`, `ha apps rebuild local_cognitive_core
--force` succeeded; `ha apps info` shows option `observation_mode: shadow`
and the schema as a list: `observation_mode` select `[shadow, active]`. The
Supervisor API reports `schema` as a **list**, not a mapping, so a check for
the procedure's YAML form (`{…}`) cannot match. Rebuild restores the previous
run state; the add-on was stopped after the failed attempt, so `ha apps
start local_cognitive_core` was required.

### Phases 6–7 — running container == `8570f77`: PASS
16/16 hashes OK inside the container, 16 files; `/data/options.json` =
`{"observation_mode": "shadow"}`; aiohttp `3.14.3`, charset-normalizer
`3.5.2`. Startup log, in this order (`[STORAGE]` precedes `[OPTIONS]`, the
procedure listed them the other way round): `STEP 7P schema verified (5
triggers)`, `backfill_review_cases: 0 created, 0 reconciled`,
`observation_mode=shadow`, subscribed to `device_registry_updated` and
`entity_registry_updated`, no errors. First sweep:

```
swp_c44be69d702f shadow startup
discovered=160 no_entities=14 missing_metadata=0 unchanged=0
classified=18 no_match=128 error=0
excluded_disabled_entities=357 unavailable_entities=86
```

### Phase 8 — `d2_verify.py` (cumulative): PASS
```
D0      PASS   2 resolved cases re-decided on a copy -> already_resolved, decision fields identical
D1      PASS   13/13 triggers, 13/13 exact refusals, review-only UPDATE allowed
D2      PASS   5/5 triggers, 5/5 exact refusals, unexpected triggers: none
PINS    PASS   17/17 at the pinned version
DATA    PASS   assets 13, observations 11, environmental_readings 0, review_cases 2,
               review_queue 9: +0 new, 0 changed; pre-D2 observations with input_id: 0
SHADOW  PASS   observations 11/11, review_cases 2/2, assets 13/13, classification_inputs 0
RESULT: PASS
```
Sweeps at that time (`discovered no_entities missing unchanged classified no_match error`):
```
swp_c44be69d702f shadow startup                  160 14 0   0 18 128 0
swp_c317dff3830e shadow entity_registry_updated  160 14 0 146  0   0 0
swp_8b8cf7bccefa shadow entity_registry_updated  160 14 0 146  0   0 0
```
Two real registry events arrived in the first minutes; both sweeps found all
146 devices with entities `unchanged` — the fingerprint gate holds on real
production events (no state values in the fingerprint).

### Phase 9 — `shadow_check.py`: NOT READY (expected)
```
S1 coverage          MET      3 shadow sweeps; latest discovered=160; invariant broken in: none
S2 errors            MET      errors: none
S3 probe parity      MET      swp_c44be69d702f {classified 18, matches 20, motion 4} == probe
S4 gate stability    NOT MET  no daily shadow sweep with a stored baseline at least 20 h older yet
S5 missing metadata  MET      0/146 = 0.0% (limit 5%)
S6 no writes         MET      changes since pre-D2: none
RESULT: NOT READY
```
NOT READY only because of S4, which by definition needs real elapsed time.
No extra sweeps are forced to satisfy it.

### Difference to Probe 7P: 14/128 vs 12/130 — explained
The probe counted every registry entity; 7P excludes disabled entities
(ADR §5.1; 357 on this instance). Two devices whose entities are **all
disabled** therefore move from `no_match` to `no_entities`. `classified`
(18), matches (20) and motion (4) are unchanged, which is what S3 compares.

---

## Incident — rebuild hijacked by a backup `config.yaml`

**Symptom.** First `ha apps rebuild local_cognitive_core --force` failed
with `Cannot build app because dockerfile is missing`. The rebuild had
already removed the old container and image, so the add-on was down
(`state: error`). Production data was never at risk: `/data/core.db` is in
the add-on's data volume, untouched by rebuild.

**Cause.** Supervisor 2026.09.3 discovers local apps with a recursive
`**/config.*` glob and sets the app location to the directory of the file
found. Phase 2 had copied `config.yaml` into `backup_d2/`; that copy has the
same slug, so the Supervisor resolved the app's location to `backup_d2/`,
where there is no `Dockerfile`. A second, older duplicate
(`config.step4-backup.yaml` in the add-on root) was latent and would have
caused the same class of problem. A stale `apps.json` location (#6917) was
ruled out: no location stored there.

**Fix (no uninstall, no `ha supervisor repair`, no `apps.json` edit):**
```
mv $A/backup_d2/config.yaml $A/backup_d2/pre_d2_config.yaml
mv $A/config.step4-backup.yaml $A/step4-backup_config.yaml
find $A -name 'config.*'          # -> only ./config.yaml
ha store reload
ha apps rebuild local_cognitive_core --force
ha apps start local_cognitive_core
```
Then Phases 6–9 as above. Downtime: from the failed rebuild to the start.

**Lessons (applied to every future deployment).**
1. Never place a file named `config.*` anywhere inside the add-on tree,
   backups included; name backups `pre_dX_config.yaml`.
2. Before any rebuild: `find $A -name 'config.*'` must return only
   `./config.yaml`.
3. `rebuild` removes the image before building: a failed build means the
   add-on is down; it does not restart an app that was not running.
4. `ha apps info` shows the installed snapshot, updated only by a
   successful install/update/rebuild.

---

## Rollback (corrected)

**A — code/config/requirements** — proven by
`tests/test_rollback_d1_on_d2.py`:
```
cd $A
mv src backup_d2/src_d2_failed
mv backup_d2/src_d1 src
cp backup_d2/pre_d2_config.yaml config.yaml
cp backup_d2/requirements.txt .
find $A -name 'config.*'
ha store reload
ha apps rebuild local_cognitive_core --force
```
`find` must show only `./config.yaml`. Then the Phase 1 hashes must
reappear. The D2 tables, `input_id` and the 5 triggers stay in
`/data/core.db`; the D1 code runs on them unchanged.

**B — database (break-glass only):** restore `backup_d2/pre_d2.db` with the
add-on stopped. Loses every write after Phase 2.

---

## Conclusion

STEP 7P runs in production **in shadow mode**: `8570f77` source, pinned
dependencies, 5 STEP 7P triggers on top of the 13 D1 triggers, every pre-D2
row unchanged, D0 decisions still immutable, no observation, input, review
case or asset written. Classification on the corrected input reproduces
Probe 7P exactly (18/20/4) and the fingerprint gate is stable on real
registry events. Tag `d2-deployed` (= deployed in shadow).

## Next

1. After ≥ 1 daily shadow sweep with a baseline ≥ 20 h older:
   `shadow_check.py` again.
2. All S1–S6 MET → lightweight FULL GATE, explicit **GO** →
   `observation_mode: active` in the add-on UI → restart → own attestation
   and tag `7p-active`.
3. Then STEP 7a.
