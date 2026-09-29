# Pipeline Check — HA event → classification_observations

**Status:** Verified — pipeline working end-to-end
**Date:** 2026-09-29 (event fired 09:02:11 UTC / 11:02 CEST)
**Production code:** `ad483f5` (deployed by D0, `docs/D0-DEPLOYMENT.md`)
**Tool:** `scripts/pipeline_probe.py` @ `5d3b718`
(SHA256 `2d696bda272dc1a74af310c9147863334fb11b4949c923b6a9e96b74c0c5fa04`, LF)

---

## Question

`/data/core.db` had not been written since 2026-08-08. Before building
STEP 7a on top of the observation stream: is the absence of new
observations **expected** (no qualifying events) or a **broken pipeline**?

## Answer

**Expected. The pipeline works; it simply receives almost no input.**

Cognitive Core reacts to exactly one Home Assistant event:
`device_registry_updated` with `action` in `{create, update}`
(`src/adapters/ha.py`). HA emits it only when a **device registry entry**
changes — device added, renamed, moved to another area, firmware version
updated, reconfigured. Sensor readings and state changes do **not** emit it.
There is no startup or periodic scan of existing devices.

Evidence from the M0 baseline: all 10 observations ever recorded were
created on 2026-08-07 between 19:32 and 20:09 UTC — the STEP 5 test session,
in which registry events were triggered by hand. There is not a single
observation outside that session. Each event produced exactly one
observation (10 observation groups, 10 rows). After the D0 rebuild, the
container log contained **0** `[HA DEBUG]` lines until the test below — no
registry event arrived at all.

---

## Code path and what can stop it

```
HA WebSocket  device_registry_updated (action create|update)
  → HAAdapter._handle_message        prints [HA DEBUG] event action=… device_id=…
  → HAAdapter._get_device            config/device_registry/list + entity_registry/list
  → CognitiveCore.on_device_detected
  → CognitiveCore.classify_device    3 classifiers
      for each classifier returning a result:
        → Storage.log_classification_observation   (INSERT)
        → Storage.upsert_review_case                (no confidence threshold)
  → Storage.save_asset                              (only if a best hypothesis exists)
```

An observation is **not** created when:
1. no `device_registry_updated` event with `create`/`update` arrives
   (`remove` is ignored);
2. `_get_device()` does not find the device or raises (swallowed, logs
   `[HA] Error getting device`);
3. no classifier matches (logs `No classifier matched`; nothing is stored —
   of 5 Zigbee devices in HA, only 2 have ever been classified);
4. the INSERT fails (`log_classification_observation` returns `None`,
   logs the error);
5. the WebSocket is down (reconnect loop logs `[HA] Connection error`).

**STEP 6.2 did not touch this path.** In `core.py` it removed only the
`add_to_review_queue()` call after `save_asset()`. `src/adapters/ha.py` is
unchanged since `da07aa8` (STEP 5, 2026-08-07).

---

## Controlled test

**Trigger:** one synthetic event, fired once from HA → Developer Tools →
Events. The event is synthetic; the device data Cognitive Core processes
is real (it re-reads the current registry entry). The device itself was not
changed.

```yaml
# event type: device_registry_updated
action: update
device_id: d362c2797c36666d6ad15bdf39e2e97e   # T & H Sensor, key of case_8dc45785
changes: {}                                   # no field changed → other HA listeners have nothing to act on
```

### Before

```
classification_observations: 10
assets: 12
review_cases pending: 0
review_cases resolved: 2
newest observation: obs_5abf1edbc225 @ 2026-08-07T20:09:17.166876Z
  case_8dc45785 | resolved | approved | obs_5abf1edbc225 | 2026-08-07T20:09:17.252602Z
  case_a35cab51 | resolved | approved | obs_b22058a8cfbf | 2026-08-08T20:19:43.895131Z
[HA DEBUG] lines since D0 rebuild: 0
```

### Log

```
[HA DEBUG] event action=update device_id=d362c2797c36666d6ad15bdf39e2e97e
[CORE] Device detected: T & H Sensor
[CORE] Result from energy_meter: None
[CORE] Result from environmental_sensor: {'category': 'environmental_sensor', ...
        'device_classes': ['temperature', 'humidity', 'battery']}
[STORAGE] Observation logged: obs_ccb5f9011880 (environmental_sensor @ 70%)
[STORAGE] Review case upserted: case_8dc45785
[CORE] Result from motion_sensor: None
[CORE] Hypothesis: environmental_sensor (0.7)
[STORAGE] Saved asset: asset_8bd0c6ec
[CORE] Asset saved: asset_8bd0c6ec
```

No errors.

### After

```
classification_observations: 11
assets: 13
review_cases pending: 0
review_cases resolved: 2
newest observation: obs_ccb5f9011880 @ 2026-09-29T09:02:11.466726Z
  case_8dc45785 | resolved | approved | obs_ccb5f9011880 | 2026-09-29T09:02:11.571939Z
  case_a35cab51 | resolved | approved | obs_b22058a8cfbf | 2026-08-08T20:19:43.895131Z
```

### Result

| Criterion | Expected | Observed | |
|---|---|---|---|
| event reaches Cognitive Core | `[HA DEBUG] … d362c279…` | yes | ✅ |
| device fetched | `Device detected: T & H Sensor` | yes | ✅ |
| hypothesis | `environmental_sensor` | `environmental_sensor (0.7)` | ✅ |
| observations | 10 → 11 | 10 → 11 | ✅ |
| assets | 12 → 13 | 12 → 13 | ✅ |
| review cases | 0 pending / 2 resolved | 0 / 2 | ✅ |
| `case_8dc45785` | resolved/approved, pointer advances | `obs_ccb5f9011880`, still approved | ✅ |

`Review case upserted: case_8dc45785` on an already-resolved case is the
intended STEP 5 behaviour: the logical case receives fresher evidence
(`last_observation_id`, `updated_at`) but is **not reopened**, and — since
D0 — its decision cannot be overwritten.

Classification is stable since August: all 8 August observations of this
device also had confidence 0.7 and 3 entities.

### Intended production writes

The test changed the production database exactly as intended:
- +1 `classification_observations` row (`obs_ccb5f9011880`)
- +1 `assets` row (`asset_8bd0c6ec`)
- `case_8dc45785.last_observation_id` / `updated_at` advanced (by design)

No human decision, no review case count, and no other row was changed.
All writes are additive except the evidence pointer, which is not a
decision field. The M0 baseline therefore no longer equals the live
database; M0 remains the pre-D0 reference.

---

## Findings

1. **Correction to `D0-DEPLOYMENT.md`:** the last pre-test database write
   (2026-08-08 22:19 CEST) is exactly the resolution of `case_a35cab51`
   (`decided_at` 2026-08-08T20:19:43.895131Z), not "sometime between 10:17
   and 22:19". See the erratum appended there.
2. **Raw device data is not persisted.** `assets.device_data` stores the
   asset dict (id, name, hypothesis, source), not the device's entities;
   observations keep only `device_entity_count`. Classifications cannot be
   replayed offline from the database. Relevant for STEP 5 "Level 2"
   knowledge (correlating corrections with device attributes).

## Consequence for STEP 7a

The observation stream is correct but nearly empty: registry changes are
rare, so a shadow-mode precedent layer would produce almost no annotations.
Before STEP 7a, the **source of observations** needs a deliberate decision
(e.g. a startup and/or periodic inventory scan). That is an architecture
decision with its own trade-off — a scan re-observes every device, which is
the repeated-rows problem (10 raw rows : 1 judgment) already identified in
STEP 6 — and is **not** part of M1/D1.

## Future architecture topic (recorded, not scheduled)

Should Cognitive Core ever react to **state changes / sensor observations**,
not only to device registry changes? Deliberately kept out of M1 and
STEP 7a; to be taken up as its own design question.

## Cleanup

`/tmp/pipeline_probe.py` in the container is temporary and disappears on
the next rebuild. `/share/d0/pipeline_probe.py` is retained with the D0
package.
