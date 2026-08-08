# STEP 6.1 — Deployment Attestation

**Status:** Deployment Verified
**Date:** 2026-08-08
**Commit:** `f7dff9a06e5df7b8dcfe22d936a37eb14816ffe8`
**Tag:** `step6-complete` (code checkpoint — see note on scope below)
**Artifact:** `src/storage.py`

---

## Why this document exists

`step5-complete` and `step6-complete` are Git checkpoints — proof of what
code exists and was tested locally. Neither, by itself, proves that code
was ever running on HAOS. This document is that separate proof: not "what
was committed," but "what was actually deployed, and how do we know the
running artifact matches the approved one."

The distinction matters going forward:

```
STEP 5    → committed AND production-verified (da07aa8, deployed + E2E tested)
STEP 6    → committed only (f7dff9a, local tests pass)
STEP 6.1  → deployment verified separately on HAOS — this document proves
            f7dff9a's storage.py is what HAOS runs, distinct from the
            code checkpoint itself
```

Six months from now, the answerable question is not "what was the code,"
but: *was the version running on HAOS actually the one we approved?* This
document answers that, with hashes, not memory.

---

## Scope

**Deployed:** `src/storage.py` only — specifically, the addition of
`Storage.get_correction_patterns()` (STEP 6, `docs/STEP5_ARCHITECTURE.md`'s
originally-planned Level 1 learning analytics, implemented under the
"STEP 6" name in this repo's history — see that doc and the STEP 6 audit
in project conversation history for the naming context).

**Explicitly NOT deployed or changed in this step:**
- `src/core.py` — no call site added
- `src/main.py` — no CLI command added
- `src/adapters/ha.py` — unchanged (still `action in {"create", "update"}`
  from `step5-complete`)
- Database schema — no migration; `get_correction_patterns()` reads
  existing `classification_observations` columns only
- Any classifier behavior, confidence, or rule

`get_correction_patterns()` is present in the running container's code but
has zero callers anywhere in the application. It is dead code from the
runtime's perspective — reachable only via a direct `Storage` instance
call that nothing in `main.py`/`core.py` currently makes. This was the
explicit goal of STEP 6.1: prove the artifact can be deployed with no
behavioral change, before any future step wires it into a CLI or report.

---

## SHA256 chain of custody

```
Windows working tree (C:\Users\marek\Documents\cognitive_core\src\storage.py)
  verified: git diff --exit-code HEAD -- src/storage.py  →  no output (clean)
  SHA256:   B67A37B6215C0A31BAC317E2D3552A90437457A6E6C652E74F81CDC9903EDD6E

HAOS source (/mnt/data/supervisor/apps/local/cognitive_core/src/storage.py)
  transferred via HTTP bridge (python -m http.server on Windows,
  urllib.request.urlretrieve inside the container to /tmp, then
  docker cp / rebuild picked up the source directory)
  SHA256:   b67a37b6215c0a31bac317e2d3552a90437457a6e6c652e74f81cdc9903edd6e

Runtime (/app/src/storage.py inside app_local_cognitive_core, post-rebuild)
  command:  docker exec app_local_cognitive_core sha256sum /app/src/storage.py
  SHA256:   b67a37b6215c0a31bac317e2d3552a90437457a6e6c652e74f81cdc9903edd6e
```

All three match (case-insensitive hex comparison). The file running in
production is byte-for-byte the file committed at `f7dff9a`.

---

## Pre-deployment rollback point

```
/mnt/data/supervisor/apps/local/cognitive_core/storage_step5_backup.py
SHA256: 65e94f765d4f42f80732e8b468e5b055c09be3245d9441decf17eaadc8c5b5ad
```

This is the `storage.py` that was running immediately before this
deployment (`step5-complete`'s version, with `review_cases` but without
`get_correction_patterns()`). Stored outside `src/` deliberately, so it is
never picked up as part of the application source during a future build.

**Rollback procedure, if ever needed:**
```bash
cp /mnt/data/supervisor/apps/local/cognitive_core/storage_step5_backup.py \
   /mnt/data/supervisor/apps/local/cognitive_core/src/storage.py
ha apps rebuild local_cognitive_core --force
docker exec app_local_cognitive_core sha256sum /app/src/storage.py
# expect: 65e94f765d4f42f80732e8b468e5b055c09be3245d9441decf17eaadc8c5b5ad
```

---

## Pre-deployment runtime baseline check

Before touching anything, the actual communication paths were verified
rather than assumed from prior project documentation (which had described
Zigbee2MQTT as abandoned in favor of ZHA — that turned out to be only
partially still true for the HAOS host in general, though irrelevant to
Cognitive Core specifically):

```
ZHA integration:              active in Home Assistant (5 Zigbee devices)
Mosquitto broker:              running on HAOS
Zigbee2MQTT app:                running on HAOS
Cognitive Core → MQTT/Mosquitto: ZERO references
  command: grep -Rni "mqtt\|mosquitto" \
           /mnt/data/supervisor/apps/local/cognitive_core/src
  result:  no output, exit code 1
Cognitive Core → HA WebSocket:  confirmed via startup log
  ([HA] Connected / Authenticated / Subscribed to device_registry_updated)
```

Conclusion: Mosquitto and Zigbee2MQTT are active components of the HAOS
host in general, but are not part of Cognitive Core's communication path.
Cognitive Core talks to Home Assistant exclusively via the Supervisor
WebSocket API. This should not be re-assumed from prior docs in future
sessions — re-verify if the HAOS integration topology is ever in question
again.

---

## Post-deployment verification

```bash
ha apps rebuild local_cognitive_core --force
# Command completed successfully.

docker ps --format "table {{.ID}}\t{{.Image}}\t{{.Names}}\t{{.Status}}"
# app_local_cognitive_core   Up 14 minutes

ha apps info local_cognitive_core --raw-json
# "state":"started", "version":"0.2.0"

docker logs app_local_cognitive_core 2>&1 | \
  grep -E "Starting as|Database:|review_cases|Connected|Authenticated|Subscribed|ERROR|Traceback"
```

Startup log (no `ERROR`/`Traceback` present):

```
[COGNITIVE_CORE] Starting as Home Assistant App
[COGNITIVE_CORE] Database: /data/core.db
[STORAGE] Initializing review_cases schema...
[STORAGE] Table review_cases created/verified
[STORAGE] review_cases schema initialization complete - OK
[STORAGE] backfill_review_cases: 0 created, 0 reconciled
[CORE] Starting Cognitive Core 0.1... (Build: 2026-08-01-step2-ha-app)
[CORE] Loaded 3 classifiers
[CORE] Loaded 1 adapters
[CORE] Ready
[HA] Connecting to ws://supervisor/core/websocket...
[HA] Connected
[HA] Authenticated
[HA] Subscribed to device_registry_updated
[HA_SENSOR] Published sensor.cognitive_core_pending_reviews = 1
```

## Regression checklist

| Check | Result |
|---|---|
| Container starts and stays up | ✅ `Up 14 minutes` |
| `review_cases` schema initializes | ✅ no errors |
| `backfill_review_cases()` runs | ✅ `0 created, 0 reconciled` (no new data since STEP 5, expected) |
| HA WebSocket connects/authenticates/subscribes | ✅ |
| `sensor.cognitive_core_pending_reviews` publishes | ✅ value `1` (pre-existing, unrelated to this deployment — see note below) |
| No `ERROR`/`Traceback` in startup log | ✅ |
| `get_correction_patterns()` has any active call site | ✅ confirmed absent — zero callers in `core.py`/`main.py` |

**Note on `pending_reviews = 1`:** this value predates this deployment
(known open review case from the T & H Sensor testing during STEP 5) and
is unaffected by this change. Recorded here so a future reader doesn't
mistake it for a STEP 6.1 side effect.

---

## Conclusion

`f7dff9a`'s `src/storage.py` is confirmed running in production
(`app_local_cognitive_core`), verified by matching SHA256 across working
tree, HAOS source, and container runtime. `get_correction_patterns()` is
live but has no callers, so this deployment made zero observable change
to Cognitive Core's behavior — exactly the intended outcome of a
verification-only deployment step.

## Next logical milestone

Not automatic. `get_correction_patterns()` remains passive by design.
The next decision point is deliberate: whether and how correction-pattern
output should ever be surfaced (CLI report, export, or otherwise) — and
that remains strictly Level 1 ("statistics," per `docs/STEP5_ARCHITECTURE.md`'s
five-level table) unless a separate, equally deliberate design step
decides to go further. No classifier, confidence, or rule is to be
modified automatically by Cognitive Core itself, now or under any future
step, without a dedicated architecture decision at the same level of
scrutiny as this one.

---

*Deployment attestation, written immediately after production verification
on 2026-08-08. Companion to `step6-complete` (code checkpoint) — read both
together, they answer different questions.*
