# D1 — M1 deployment (database-level immutability)

**Status:** Deployment verified — PASS
**Date:** 2026-09-29/30
**Deployed source state:** `3277d9a` (`m1-complete`) — `src/storage.py` only
**Verification tools:** `scripts/d1_verify.py` @ `78b7966`,
`scripts/d0_verify.py` @ `31bf68e` (regression), `scripts/pipeline_probe.py` @ `5d3b718`
**Predecessors:** `docs/D0-DEPLOYMENT.md`, `docs/PIPELINE-CHECK.md`,
`docs/STEP7_ARCHITECTURE.md` (M1 section)

---

## Purpose and scope

D1 puts M1 into production: the database itself refuses to change what was
recorded. Model:

```
record identity   immutable   trg_obs_immutable_id            (1 trigger)
raw hypothesis    immutable   trg_obs_immutable_<column>      (12 triggers)
review metadata   mutable     review_status, human_decision, corrected_category,
                              reviewed_at, review_reason, reviewed_by
```

Value-change semantics (`WHEN OLD.col IS NOT NEW.col`, NULL-safe): assigning
the same value is allowed, a real change aborts with
`IMMUTABLE_RAW_HYPOTHESIS: classification_observations.<col> cannot be changed
after insert` or `IMMUTABLE_OBSERVATION_IDENTITY: classification_observations.id
cannot be changed after insert`. INSERT and DELETE are not restricted.

Only `src/storage.py` changed between production (`ad483f5`, D0) and
`3277d9a`; `main.py` and `core.py` are unchanged. The triggers are created by
`Storage.init_schema()` on startup (`CREATE TRIGGER IF NOT EXISTS`), so they
live in `/data/core.db`, not in the image. No other schema change.

---

## Package

Built from git objects (`git show 3277d9a:src/storage.py`,
`git show 78b7966:scripts/d1_verify.py`), LF, each verified against its git
blob; copied to `\\homeassistant.local\share\d1` and hashed on the share.

```
6bb9c184420a45405d4b586a4b6964b2e912e049e01381080522e76d13bd3669  storage.py
53d227f4a11178225b94524b13fe23e438a26b1b46f892a0958945aa35ce003c  d1_verify.py
2bec57a270a2e32acf4e4c4dfad30b563f3c816e1eeb7ba6aea68e3c99d95c07  (SHA256SUMS)
```

Phase 0 on HAOS: `sha256sum -c SHA256SUMS` → `storage.py: OK`, `d1_verify.py: OK`.

---

## Baseline (Phase 1)

`src/` on HAOS and `/app/src` in the running container, identical — the D0
state:

```
040a50ada817ed55d2d02616abd823d7b5e6cfd1381c6ecb74e8c0e3532ebbd6  storage.py
d256a2d5c44e8c1bf00906f9e7b7145d3c13654fc78379fd155da3bb32676cec  main.py
c3a70399c9faab32956ffa837921c197b305ee1ae4e59010e55c72e4141fabb1  core.py
```

## Rollback point (Phase 2)

```
/mnt/data/supervisor/apps/local/cognitive_core/backup_d1/
  storage.py   040a50ada817ed55d2d02616abd823d7b5e6cfd1381c6ecb74e8c0e3532ebbd6
  pre_d1.db    e4763cd41bd202270e219f18f2ab9b15462a1bdfa059b752c307d72cb4cdc571
```

`pre_d1.db` taken with the SQLite backup API (`backup ok`, 106 kB), hashed
twice with identical results. It differs from M0 (`2f0ea303…`) as expected:
the pipeline check of 2026-09-29 added one observation and one asset.

**Rollback procedure:**
```bash
cd /mnt/data/supervisor/apps/local/cognitive_core
cp backup_d1/storage.py src/
sha256sum src/storage.py                       # expect 040a50ad…3532ebbd6
ha apps rebuild local_cognitive_core --force
docker exec app_local_cognitive_core sha256sum /app/src/storage.py
```
The triggers stay in `/data/core.db` after a code rollback. That is safe:
the D0 code (`ad483f5`) never updates protected columns (verified against
every `UPDATE classification_observations` statement). Removing them is a
deliberate break-glass operation (13× `DROP TRIGGER`), never part of a
routine rollback.

---

## Evidence 1 — source on HAOS == `3277d9a` (Phase 3)

```
cp $S1/storage.py src/
6bb9c184420a45405d4b586a4b6964b2e912e049e01381080522e76d13bd3669  src/storage.py   ✅ M1
d256a2d5c44e8c1bf00906f9e7b7145d3c13654fc78379fd155da3bb32676cec  src/main.py      ✅ unchanged
c3a70399c9faab32956ffa837921c197b305ee1ae4e59010e55c72e4141fabb1  src/core.py      ✅ unchanged
```
Re-checked after the rebuild with identical results.

## Evidence 2 — running container == `3277d9a` (Phases 4–5)

```
ha apps rebuild local_cognitive_core --force
Processing... Done.
Command completed successfully.

docker ps → new container bd21911e2117

docker exec app_local_cognitive_core sha256sum /app/src/storage.py /app/src/main.py /app/src/core.py
6bb9c184…d13bd3669  /app/src/storage.py   ✅
d256a2d5…b32676cec  /app/src/main.py      ✅
c3a70399…4141fabb1  /app/src/core.py      ✅
```

Startup log (complete, from the first line of the new container):
```
[COGNITIVE_CORE] Starting as Home Assistant App
[COGNITIVE_CORE] Database: /data/core.db
[STORAGE] ... classification_observations schema initialization complete - OK
[STORAGE] Raw hypothesis immutability triggers verified (12 columns)
[STORAGE] Observation identity immutability trigger verified
[STORAGE] ... review_cases schema initialization complete - OK
[STORAGE] backfill_review_cases: 0 created, 0 reconciled
[CORE] Loaded 3 classifiers / Loaded 1 adapters / Ready
[HA] Connected / Authenticated / Subscribed to device_registry_updated
[HA_SENSOR] Published sensor.cognitive_core_pending_reviews = 0
```
The triggers are created right after `classification_observations` and
before the review-column migration and the backfill; the backfill writes
only `review_cases` and ran without conflict.

## Evidence 3 — `d1_verify.py` == PASS (Phase 6)

Run inside the rebuilt container with `backup_d1/pre_d1.db` copied back to
`/tmp/pre_d1.db` (the rebuild clears `/tmp`). Live DB and `pre_d1.db` opened
read-only; UPDATE attempts only on the backup-API copy `/tmp/d1_verify.db`.

```
D1 VERIFY
deployed storage.py  6bb9c184420a45405d4b586a4b6964b2e912e049e01381080522e76d13bd3669
deployed main.py     d256a2d5c44e8c1bf00906f9e7b7145d3c13654fc78379fd155da3bb32676cec
deployed core.py     c3a70399c9faab32956ffa837921c197b305ee1ae4e59010e55c72e4141fabb1
pre_d1.db SHA256     e4763cd41bd202270e219f18f2ab9b15462a1bdfa059b752c307d72cb4cdc571

raw-hypothesis triggers: 12/12
  trg_obs_immutable_classifier_name
  trg_obs_immutable_created_at
  trg_obs_immutable_device_entity_count
  trg_obs_immutable_device_id
  trg_obs_immutable_device_manufacturer
  trg_obs_immutable_device_model
  trg_obs_immutable_device_name
  trg_obs_immutable_device_source_adapter
  trg_obs_immutable_hypothesis_category
  trg_obs_immutable_hypothesis_confidence
  trg_obs_immutable_hypothesis_reasoning
  trg_obs_immutable_observation_group_id
observation-identity trigger: present
unexpected triggers: none

Rows from pre_d1.db in the live database:
  assets: 13 snapshot rows checked, +0 new, 0 pointer(s) advanced
  classification_observations: 11 snapshot rows checked, +0 new, 0 pointer(s) advanced
  environmental_readings: 0 snapshot rows checked, +0 new, 0 pointer(s) advanced
  review_cases: 2 snapshot rows checked, +0 new, 0 pointer(s) advanced
  review_queue: 9 snapshot rows checked, +0 new, 0 pointer(s) advanced

Enforcement (on a copy):
  protected-column UPDATEs refused with exact message: 13/13
  review-only UPDATE allowed: yes

RESULT: PASS
```

Adding the triggers on startup changed **no row** in any table.

### Negative control (performed before D1)

`d1_verify.py` against a duplicate of the M0 baseline started with the
current production code `ad483f5`: **FAIL** — 13 missing triggers and 13
protected UPDATEs not refused. With `3277d9a`: PASS. The check therefore
detects the absence of protection rather than passing unconditionally.

## Regression — `d0_verify.py` == PASS

Human-decision immutability (D0) still holds with the M1 code:

```
D0 VERIFY
deployed storage.py  6bb9c184420a45405d4b586a4b6964b2e912e049e01381080522e76d13bd3669
live review_cases: {'resolved': 2}  (read-only)
work copy: /tmp/d0_verify.db  resolved cases snapshotted: 2
  resolve_review_case(case_8dc45785, rejected) -> already_resolved
  legacy decision on obs_38db67ece910 -> already_resolved
  resolve_review_case(case_a35cab51, rejected) -> already_resolved
  legacy decision on obs_b22058a8cfbf -> already_resolved
[PASS] all decision fields identical before/after (2 cases)
RESULT: PASS
```

## Final production check

```
pipeline_probe.py
  classification_observations: 11
  assets: 13
  review_cases pending: 0
  review_cases resolved: 2
  newest observation: obs_ccb5f9011880 @ 2026-09-29T09:02:11.466726Z
  case_8dc45785 | resolved | approved | obs_ccb5f9011880 | 2026-09-29T09:02:11.571939Z
  case_a35cab51 | resolved | approved | obs_b22058a8cfbf | 2026-08-08T20:19:43.895131Z

docker logs app_local_cognitive_core 2>&1 | grep -c -E "ERROR|Traceback"   → 0
docker exec app_local_cognitive_core sha256sum /app/src/storage.py
  6bb9c184420a45405d4b586a4b6964b2e912e049e01381080522e76d13bd3669
```

Identical to the state after the pipeline check: D1 and its verification
wrote nothing to the live database apart from the trigger definitions.

---

## Operator notes

- Two `docker cp` commands were first mistyped (container name
  `app+local+cognitive_core`; earlier `.pl` for `.py`) and failed with
  "No such container" / "no such file"; nothing was copied. Both were
  repeated correctly.
- Temporary files left in the container's `/tmp` (`pre_d1.db`,
  `d1_verify.py`, `d1_verify.db`, `d0_verify.py`, `d0_verify.db`,
  `pipeline_probe.py`) are discarded on the next rebuild; optional cleanup:
  `docker exec app_local_cognitive_core rm -f /tmp/pre_d1.db /tmp/d1_verify.py /tmp/d1_verify.db /tmp/d0_verify.py /tmp/d0_verify.db /tmp/pipeline_probe.py`
- `backup_d1/` and `/share/d1` are retained (rollback point, deployed package).

---

## Conclusion

```
source on HAOS    == 3277d9a    (Evidence 1)
running container == 3277d9a    (Evidence 2)
d1_verify         == PASS       (Evidence 3)
d0_verify         == PASS       (regression)
production state  unchanged     (final check)
```

The production database now enforces its own history: an observation's
identity and raw classifier output cannot be changed after insert, and
(since D0) a human decision cannot be overwritten. Tagged `d1-deployed`.

## Next

Architecture decision before STEP 7a: the **source of observations**. The
only trigger today is `device_registry_updated`, so the stream is sparse
(`docs/PIPELINE-CHECK.md`). Options to decide in a document before code:
periodic registry resync, `entity_registry_updated`, or `state_changed` as a
separate behavioural-evidence stream. Note: `PIPELINE-CHECK.md` recorded
state-change handling as a future topic *outside* STEP 7a; choosing it as
the 7a source would amend that decision and `docs/STEP7_ARCHITECTURE.md`
explicitly.
