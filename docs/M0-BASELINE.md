# M0 — Production Database Baseline

**Status:** Verified — CLEAN
**Date:** 2026-09-28 (snapshot taken 21:58 CEST)
**Purpose:** a trusted, hash-identified image of the production database
taken **before** any of the new immutability protections (D0: STEP 6.3 +
6.4, D1: M1 raw-hypothesis trigger) were deployed to HAOS.

---

## What a clean M0 means — and what it does not

A clean result means: **no detectable inconsistencies or overwrites remain
in the current database state.**

It does **not** prove that historical overwrites never occurred. Before
STEP 6.3, `resolve_review_case()` could re-resolve an already resolved case,
updating `review_cases` and the labelled observation consistently and
leaving no reconstructable trace (the review CLI runs via `docker exec`, so
its output never reached `docker logs` either). That history cannot be
recovered.

M0 is therefore the **trusted baseline from this point forward**, not a
proof of historical immutability. D0 is what protects history from here on.

---

## Snapshot procedure

All steps read-only with respect to `/data/core.db`; the app kept running.

```bash
# HAOS OS shell (physical console, `login` at the ha > prompt)
docker exec app_local_cognitive_core python3 -c "import sqlite3;sqlite3.connect('/data/core.db').backup(sqlite3.connect('/tmp/core_m0.db'))"
docker exec app_local_cognitive_core sha256sum /tmp/core_m0.db
docker cp app_local_cognitive_core:/tmp/core_m0.db /mnt/data/supervisor/share/core_m0.db
```

```powershell
# Windows, via the Samba share
Copy-Item \\homeassistant.local\share\core_m0.db C:\Users\marek\Documents\cognitive_core_m0\core_m0.db
Get-FileHash C:\Users\marek\Documents\cognitive_core_m0\core_m0.db -Algorithm SHA256
```

The SQLite online backup API was used instead of copying the live file, so
the snapshot is transactionally consistent even with the app running.

## SHA256 chain of custody

```
Container copy   /tmp/core_m0.db (inside app_local_cognitive_core)
                 2f0ea3032ceaf258bfab6dc01044daaebdcbbdd6247225325d42dd661373dcac
                 (read from a photo of the console)

HAOS share       /mnt/data/supervisor/share/core_m0.db
                 not recorded (docker cp reported 106 kB copied)

Windows copy     C:\Users\marek\Documents\cognitive_core_m0\core_m0.db
                 2F0EA3032CEAF258BFAB6DC01044DAAEBDCBBDD6247225325D42DD661373DCAC
                 106496 bytes
```

Container and Windows hashes match exactly (case-insensitive). The share
hop was not hashed separately; since both ends of the chain match, the
intermediate copy cannot have differed in any way that matters.

## Tool identity

```
scripts/m0_precheck.py
  commit:    d37ded4b436a785d7b10798e6369a07f0d2e53c4  (no uncommitted changes)
  git blob:  4bd037dabd623b86a4b87f78a9271e22ed61ecd5  (line-ending independent)
  SHA256:    01b0c818f2955cc2126bd2d4d62b2bf88bff7c2dcf5019414e4e08c2665b5331  (LF checkout)
```

## Precheck result

```
[PASS] PRAGMA integrity_check: ok
[PASS] logical keys with more than one labelled observation: 0
[PASS] pending cases whose evidence row is already labelled: 0
[PASS] resolved cases with no labelled observation matching their decision: 0
[PASS] review cases pointing at a missing observation: 0

[INFO] classification_observations rows: 10
[INFO] observations carrying a human decision: 2
[INFO] review_cases pending: 0
[INFO] review_cases resolved: 2
[INFO] SQLite triggers: 0

[PASS] database SHA256 unchanged by this check
RESULT: CLEAN
```

Full output: `C:\Users\marek\Documents\cognitive_core_m0\m0_precheck_output.txt`
(SHA256 `736bd0a4aa7fa83b700ed850c23d3a766d9bbcc5f8aca0d87eb822a5322ac75f`).

## Observations

- **Pending reviews changed since STEP 6.1:** the 2026-08-08 attestation
  recorded `sensor.cognitive_core_pending_reviews = 1` (the second STEP 5
  test device). M0 shows 0 pending, 2 resolved: that case was resolved
  some time between 2026-08-08 and 2026-09-28. Both resolved cases are
  consistent with their labelled observations.
- **No triggers exist yet** — as expected before M1.
- **Scale:** 10 observations, 2 human decisions. Any STEP 7a statistic
  will start from very small numbers; this is why STEP 7a reports raw
  fractions below n = 5.

## Artifacts retained (outside the repository)

`C:\Users\marek\Documents\cognitive_core_m0\` holds `core_m0.db` (the
baseline itself) and `m0_precheck_output.txt`. Kept out of git because the
database contains device names. Do not modify either file; the baseline is
identified by the SHA256 above.
