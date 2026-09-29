# D0 — STEP 6.2–6.4 deployment

**Status:** Deployment verified — PASS
**Date:** 2026-09-28 (late evening, CEST)
**Deployed source state:** `ad483f5` (`step6.4-complete`)
**Verification tool:** `scripts/d0_verify.py` @ `31bf68e`
**Predecessors:** `docs/STEP6.1-DEPLOYMENT.md`, `docs/M0-BASELINE.md`

---

## Purpose and scope

The principal purpose of D0 is **human-decision immutability in
production**: STEP 6.3 (`resolve_review_case()` resolves only pending
cases) and STEP 6.4 (legacy `approve/reject/correct_observation()` never
overwrite an existing `human_decision`).

D0 nevertheless moves production from **STEP 6.1-era code to the
coherent, tested STEP 6.2–6.4 source state**, because those changes share
the same files and cannot be deployed separately without shipping a code
combination that never existed in `main`:

| File | Changes reaching production with D0 |
|---|---|
| `src/storage.py` | 6.2 legacy `review_queue` removal, `6d8f0ca` UTC timestamps, 6.3, 6.4 |
| `src/main.py` | 6.2 `legacy-review` removal, 6.3 `already_resolved` handling |
| `src/core.py` | 6.2: no longer calls `add_to_review_queue()` |

`core.py` is mandatory: the production `core.py` called
`storage.add_to_review_queue()`, which 6.2 removed from `storage.py`.
Deploying the new `storage.py` alone would have raised `AttributeError`
on every device detection with confidence < 0.95. A backport branch with
only 6.3/6.4 was explicitly rejected.

All other files in `src/` were verified identical between production
(`f7dff9a`) and `ad483f5` via git. No database schema change; no
migration.

---

## Actual production baseline (Phase 1)

```
                src/ on HAOS        /app/src (running)   expected (f7dff9a)
storage.py      b67a37b6…9903edd6e  identical            b67a37b6…  ✅
core.py         64a4c4ef…62338273   identical            64a4c4ef…  ✅
main.py         142add45…7e04112a   identical            0810574e…  ⚠️
```

### Finding: undocumented deployment of `73f4bf9`

Production `main.py` (`142add453802e83622ece44de0ee28e7f5acfb9017317e414206363a7e04112a`)
is byte-identical to `main.py` at commit `73f4bf9` "Add correction
patterns CLI" (2026-08-08 22:06). That deployment happened after the
STEP 6.1 attestation (same day, 10:17) and was never documented;
`STEP6.1-DEPLOYMENT.md` was correct when written but stale hours later.
The file is a known, committed version, so D0 proceeded.

Related: `main_pre_correction_cli_backup.py` on HAOS also hashes to
`142add45…` — i.e. it contains the version **with** the CLI, not the one
before it. The true pre-CLI `main.py` (`0810574e…`) exists only in git.
`storage_step5_backup.py` = `65e94f76…c5b5ad`, matching STEP 6.1.

### Finding: no database writes since 2026-08-08

`/data/core.db` mtime: **2026-08-08 22:19**. Cognitive Core has recorded
no observation or decision since then. This narrows the M0 observation:
the second STEP 5 case was resolved on 2026-08-08 between 10:17 and 22:19.
It also means no device event has produced an observation for seven
weeks — outside D0's scope, worth investigating separately.
`/data/options.json` (2026-09-21 23:21) and the pre-D0 container uptime
(`Up 6 days`) indicate the container was recreated around 2026-09-21.

---

## Rollback point (Phase 2)

```
/mnt/data/supervisor/apps/local/cognitive_core/backup_d0/
  storage.py   b67a37b6215c0a31bac317e2d3552a90437457a6e6c652e74f81cdc9903edd6e
  main.py      142add453802e83622ece44de0ee28e7f5acfb9017317e414206363a7e04112a
  core.py      64a4c4ef6c774d0eb4549a58138b901792ab64e65c3d9639416809b462338273
  pre_d0.db    0964c9648f2165da9ce05a65678eafa89fcd8711500b24ccf45178d6af0a9d1d  (read from photo)
```

`backup_d0/` stays on HAOS as the rollback point. Outside `src/`, so never
part of an image build.

**Open item:** the database was unchanged since 2026-08-08, yet `pre_d0.db`
(`0964c964…`) and the M0 copy (`2f0ea303…`) hash differently. Possible
causes: a transcription error from the photo, or header metadata written
by the backup API. Not blocking (safety copy only); to be resolved by a
logical comparison of both copies.

**Rollback procedure:**
```bash
cd /mnt/data/supervisor/apps/local/cognitive_core
cp backup_d0/storage.py backup_d0/main.py backup_d0/core.py src/
sha256sum src/storage.py src/main.py src/core.py      # expect b67a37b6 / 142add45 / 64a4c4ef
ha apps rebuild local_cognitive_core --force
docker exec app_local_cognitive_core sha256sum /app/src/storage.py /app/src/main.py /app/src/core.py
```
The database is **not** restored automatically: D0 does not change it,
and restoring would discard anything recorded since the copy.

---

## Deployment package

Built from git objects (`git show ad483f5:src/…`, `git show
31bf68e:scripts/d0_verify.py`), not from the working tree: the Windows
working tree has mixed line endings (`core.py` CRLF there), production is
LF. All files LF, each verified against its git blob.

```
040a50ada817ed55d2d02616abd823d7b5e6cfd1381c6ecb74e8c0e3532ebbd6  storage.py
d256a2d5c44e8c1bf00906f9e7b7145d3c13654fc78379fd155da3bb32676cec  main.py
c3a70399c9faab32956ffa837921c197b305ee1ae4e59010e55c72e4141fabb1  core.py
05e4439aa84392d6710966010336307d663535790caa1a62236f0100af3e2dd7  d0_verify.py
bed34adb61917472393e3af4c3f42781d3dc0927f04c55fdcc31d92040342f85  (SHA256SUMS)
```

Chain: Windows package → `\\homeassistant.local\share\d0` (hashed from
Windows after copy) → HAOS `/mnt/data/supervisor/share/d0`
(`sha256sum -c SHA256SUMS`: 4 × OK, and `sha256sum $S/*` all matching).

---

## Evidence 1 — source on HAOS == `ad483f5` (Phase 3)

```
cp $S/storage.py $S/main.py $S/core.py src/
040a50ad…3532ebbd6  src/storage.py   ✅
d256a2d5…b32676cec  src/main.py      ✅
c3a70399…4141fabb1  src/core.py      ✅
```

## Evidence 2 — running container == `ad483f5` (Phases 4–5)

Rebuild required: the Dockerfile copies `src/` into the image
(`COPY src/ /app/src/`), so a restart alone would have kept the old code.

```
ha apps rebuild local_cognitive_core --force
docker exec app_local_cognitive_core sha256sum /app/src/storage.py /app/src/main.py /app/src/core.py
040a50ad…3532ebbd6  /app/src/storage.py   ✅
d256a2d5…b32676cec  /app/src/main.py      ✅
c3a70399…4141fabb1  /app/src/core.py      ✅
```

Startup log (new container):
```
[STORAGE] ... review_cases schema initialization complete - OK
[STORAGE] backfill_review_cases: 0 created, 0 reconciled
[CORE] Starting Cognitive Core 0.1... (Build: 2026-08-01-step2-ha-app)
[CORE] Loaded 3 classifiers
[CORE] Loaded 1 adapters
[CORE] Ready
[HA] Connected / Authenticated / Subscribed to device_registry_updated
[HA_SENSOR] Published sensor.cognitive_core_pending_reviews = 0   (then every 60 s, ~22× observed)
```
No `ERROR`/`Traceback` was reported by the operator on reviewing the full
log. The count command (`grep -c -E "ERROR|Traceback"`) was not captured
on a photo; the visible startup sequence is clean.

(The build label `2026-08-01-step2-ha-app` is a hard-coded string in
`core.py`, not a build timestamp; the hashes above are authoritative.)

## Evidence 3 — `d0_verify.py` == PASS (Phase 6)

Run inside the rebuilt container. Reads the live database read-only,
copies it via the SQLite backup API to `/tmp/d0_verify.db`, and runs the
**deployed** `/app/src` code against the copy only.

```
D0 VERIFY
deployed storage.py  040a50ada817ed55d2d02616abd823d7b5e6cfd1381c6ecb74e8c0e3532ebbd6
deployed main.py     d256a2d5c44e8c1bf00906f9e7b7145d3c13654fc78379fd155da3bb32676cec
deployed core.py     c3a70399c9faab32956ffa837921c197b305ee1ae4e59010e55c72e4141fabb1

live review_cases: {'resolved': 2}  (read-only)
work copy: /tmp/d0_verify.db  resolved cases snapshotted: 2

Overwrite attempts (on the copy):
  resolve_review_case(case_8dc45785, rejected) -> already_resolved
  legacy decision on obs_38db67ece910 -> already_resolved     (reject_observation)
  resolve_review_case(case_a35cab51, rejected) -> already_resolved
  legacy decision on obs_b22058a8cfbf -> already_resolved     (reject_observation)

[PASS] all decision fields identical before/after (2 cases)

RESULT: PASS
```

Production state after D0: **0 pending / 2 resolved**; both human
decisions are `approved`.

### Negative control (performed before D0)

The same script, run on a duplicate of the M0 baseline with the
**pre-D0 production code** (`f7dff9a` source tree):

```
resolve_review_case(case_8dc45785, rejected) -> resolved
legacy decision on obs_38db67ece910 -> True
resolve_review_case(case_a35cab51, rejected) -> resolved
legacy decision on obs_b22058a8cfbf -> True
[FAIL] all decision fields identical before/after (2 cases)
RESULT: FAIL
```

This demonstrates that (a) the check genuinely detects the vulnerability
rather than passing unconditionally, and (b) until D0, production code
**would have overwritten both real human decisions** had a second
resolution been attempted. The M0 evidence archive was not touched; the
dry run used a copy.

---

## Cleanup

```
docker exec app_local_cognitive_core rm -f /tmp/d0_verify.py /tmp/d0_verify.db /tmp/pre_d0.db
```
Temporary files only; `/tmp` is also discarded on the next rebuild.
`backup_d0/` and `/share/d0` are retained (rollback point and deployed
package).

---

## Conclusion

All three independent facts are established:

```
source on HAOS   == ad483f5   (Evidence 1)
running container == ad483f5  (Evidence 2)
d0_verify        == PASS      (Evidence 3)
```

Human decisions are now immutable in production across both the current
(`resolve_review_case`) and legacy (`*_observation`) write paths. Tagged
`d0-deployed`, which marks the verified production state, not merely
code readiness.

## Next

M1 / D1 — raw classifier hypothesis immutability at the database level
(`docs/STEP7_ARCHITECTURE.md`), then STEP 7a.

Open items carried forward:
1. `pre_d0.db` vs M0 hash difference (logical comparison).
2. No observations recorded since 2026-08-08 — check whether HA device
   events still reach Cognitive Core.
3. Old files in the HAOS share root (`storage.py`, `core.py`, `main.py`
   from STEP 1/2 and others) — tidy up to avoid copy mistakes.

---

## Erratum (2026-09-29)

The finding "the second STEP 5 case was resolved on 2026-08-08 between
10:17 and 22:19" is more precise than stated: the `/data/core.db` mtime of
2026-08-08 22:19 CEST **is** that resolution — `case_a35cab51.decided_at`
= 2026-08-08T20:19:43.895131Z. The "no device event for seven weeks"
observation was investigated in `docs/PIPELINE-CHECK.md`: expected
behaviour (only device registry changes trigger observations), pipeline
verified working.
