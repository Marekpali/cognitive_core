# STEP 7P — Observation Sources and Coverage (Architecture Decision Record)

**Status:** Accepted 2026-09-30. Implementation: STEP 7P (one package).
**Date:** 2026-09-30
**Precondition:** `d1-deployed` — human decisions (D0), raw hypothesis and
observation identity (D1) are immutable in production.
**Evidence:** Probe 7P (`scripts/ha_input_probe.py` @ `42220ff`), run
read-only on production 2026-09-30; report SHA256
`40a87cee273de6ada8be728b0f6b15fa2c81951098fe117b23480b921e0fe8b2`.
**Related:** `docs/STEP7_ARCHITECTURE.md` (7a precedent memory — unchanged by
this ADR), `docs/PIPELINE-CHECK.md` (why the stream is sparse).

---

## 1. Decision in one paragraph

`classification_observations` holds only classifier hypotheses computed from
a device's **structure** (device registry entry + its entities). Behavioural
data (state history) never enters it. Before STEP 7a, Cognitive Core gets
**full, measured coverage**: the classifier input is fixed, every device is
swept on startup and daily and on registry events, all sources pass one
**fingerprint gate** built from stable classifier inputs only, and every
device's outcome — including `no_match` and missing metadata — is accounted
for **outside** `classification_observations`. The first production run is
**shadow** (no inputs, observations or review cases written). STEP 7a is
unchanged.

---

## 2. Definitions

**Classification observation** — a classifier's hypothesis about what a
device is, computed from a snapshot of the device's structure. Immutable
(D1), feeds `review_cases` (STEP 5), judged by a human, consumed by STEP 7a.
Changes only when the structure or a classifier changes — never when a
sensor reading changes.

**Behavioural evidence** — what a device does over time (value ranges,
update frequency, on/off patterns) derived from state history. Not a
hypothesis, never reviewed, only meaningful aggregated. **Not an input to
STEP 7a.** Out of scope here (§9).

**Coverage** — for every device Home Assistant knows, which outcome the
last attempt had and why. Coverage is bookkeeping, not an observation.

---

## 3. Evidence: Probe 7P (production, read-only)

The probe opened its own WebSocket connection, read the device registry,
entity registry and `get_states`, ran the **real** `HAAdapter._enrich_device`
from the deployed code, and ran the three classifiers twice: on the current
adapter input and on a hypothetical input (entity-id domain + device class).
It did not touch `/data/core.db`.

### 3.1 Numbers

| | current adapter | corrected input |
|---|---|---|
| Devices discovered | 160 | 160 |
| Devices with entities | 148 | 148 |
| Devices classified | **16** (10.8 %) | **18** (12.2 %) |
| Devices `no_match` | 132 | 130 |
| Devices whose result changes | — | **4** |
| Classifier matches (device × classifier) | 16 | 20 |

Matches by classifier, current → corrected: `energy_meter` 9 → 9,
`environmental_sensor` 7 → 7, `motion_sensor` **0 → 4**.

**Why 4 changed results but only +2 classified devices.** The four changed
devices all gain `motion_sensor`:

- 2 devices (both HOBEIAN ZG-204Z, ZHA) go `no_match → motion_sensor` —
  **newly classified** (+2).
- 2 devices (the Tuya TS0601 multisensor and one unbranded ZHA
  multisensor) were already `environmental_sensor` and become
  `environmental_sensor + motion_sensor` — **already counted** as classified,
  so the device count does not move; the match count grows by 2.

Hence 4 changed results, +4 classifier matches, +2 classified devices. No
existing match was lost; all 4 new motion matches are real motion sensors
(`binary_sensor` with `device_class: motion`).

### 3.2 Confirmed: the adapter input is lossy

`HAAdapter._enrich_device()` passes each entity as `{name, domain,
entity_id}`, where `domain` is the integration **platform** (`zha`, `tuya`,
…), and passes no device class at all. Consequently
`MotionSensorClassifier` (requires `device_class == "motion"` or domain
`binary_sensor`) **cannot match in production**. Confirmed on all 4 motion
devices present.

### 3.3 New: the entity registry list carries no device class

`config/entity_registry/list` on this instance returns 21 keys per entity
(`area_id, categories, config_entry_id, config_subentry_id, created_at,
device_id, disabled_by, entity_category, entity_id, has_entity_name,
hidden_by, icon, id, labels, modified_at, name, options, original_name,
platform, translation_key, unique_id`) — **no `device_class` and no
`original_device_class`**. Of 833 entities, 0 had a registry device class;
200 had `attributes.device_class` in `get_states`.

→ On this instance the only source of device class is
`get_states[].attributes.device_class` (§5.2).

### 3.4 `no_match` is the normal outcome

130 of 148 devices match no classifier — mostly outside the scope of the
three classifiers: network clients (most of the 66 devices without integration
identifiers, MAC-named), Home Assistant apps/services, AI conversation agents,
media players. Recording `no_match` as observations would add ~130 rows of
non-hypotheses per sweep to an immutable table. It is recorded as coverage
(§6).

### 3.5 Classifier-rule gaps (not addressed by 7P)

With the corrected input some devices still match nothing although their
structure is informative: 5 WiZ bulbs and a BleBox switch with `power`, a
dishwasher with `energy`/`power`/`water`, a network switch with
`temperature`, a SmartThings `presence` entity (no classifier exists). These
are gaps in classifier **rules**, not in the input. Recorded as a separate
future topic; **7P does not extend classifiers.**

---

## 4. What STEP 7a needs (unchanged)

7a consumes exactly (1) new classification observations to annotate and
(2) human decisions on them. It does not consume sensor states. Its binding
constraint is how many distinct devices have been classified and judged —
today 2 devices, 2 decisions. 7P raises that ceiling by making every device
visible; it does not change 7a's design. A single household stays at small
sample sizes; 7a's realistic value is device-precedent recall and
auditability.

---

## 5. Classifier input (fix)

### 5.1 Entity view passed to classifiers

For every **enabled** entity of the device (`disabled_by` is null):

| Field | Source |
|---|---|
| `entity_id` | entity registry |
| `domain` | prefix of `entity_id` (e.g. `binary_sensor`) — **not** `platform` |
| `name` | registry `name`, else `original_name` (as today) |
| `device_class` | `get_states[entity_id].attributes.device_class` (§5.2) |

Disabled entities have no state in Home Assistant and are excluded from the
input; their count is kept in the coverage record for audit. `platform` is
not passed as `domain` any more.

Device fields passed to classifiers are unchanged: `id`, `name`,
`manufacturer`, `model`.

### 5.2 Device-class source hierarchy

1. `get_states[entity_id].attributes.device_class` — **the source on this
   instance** (probe §3.3).
2. *(Deferred)* per-entity `config/entity_registry/get` — only if shadow
   results show that (1) leaves a material number of devices without
   metadata. Not implemented in 7P.

**Missing metadata is explicit, never a silent `no_match`:**

- An enabled entity **with** a `get_states` entry but no `device_class`
  attribute → `device_class: null`. That is valid metadata ("this entity
  has no class"), and the device is classified normally.
- An enabled entity **without** a `get_states` entry → metadata is missing.
  The device's outcome is `skipped` with reason `missing_metadata`; it is
  **not** classified, **not** reported as `no_match`, does **not** advance
  the fingerprint gate, and is retried by the next sweep.
- Entities whose state is `unavailable` / `unknown` are counted separately
  in the shadow report (measurement only; no rule in 7P).

---

## 6. Coverage model

Every sweep accounts for every device exactly once:

```
discovered ─┬─ skipped ── reason: no_entities | missing_metadata
            └─ gated ──┬─ unchanged       (fingerprint equals the device's latest input)
                       └─ attempted ──┬─ classified  (≥ 1 classifier matched)
                                      ├─ no_match    (0 matched)
                                      └─ error       (a classifier or read failed)
```

Invariant: `discovered = skipped + unchanged + classified + no_match + error`.

| Outcome | `classification_inputs` row | observations | review cases | gate advances |
|---|---|---|---|---|
| `classified` | yes | one per matching classifier, linked by `input_id` | as today (STEP 5) | yes |
| `no_match` | yes (`outcome = no_match`) | **none** | none | yes |
| `unchanged` | no | none | none | — |
| `skipped` | no | none | none | no (retried) |
| `error` | no | none | none | no (retried) |

(Table describes **active** mode; shadow writes none of these — §8.)

Every sweep also writes one append-only row to `classification_sweeps`
(mode, source, started/finished, the counts above, excluded disabled
entities, unavailable entities, and a per-device outcome + fingerprint
list). This is where coverage is read from.

---

## 7. Canonical snapshot and fingerprint gate

### 7.1 What the fingerprint contains — and what it must not

The canonical snapshot contains **exactly the stable inputs the classifiers
read**, and nothing else:

```
device:   id, name, manufacturer, model
entities: sorted by entity_id; each { entity_id, domain, name, device_class }
```

`fingerprint = SHA256( canonical JSON of { "classifier_set_version": V,
"input": snapshot } )`, canonical JSON = sorted keys, no insignificant
whitespace, UTF-8.

**Never in the snapshot or fingerprint:** the entity **state value**
(`on/off`, temperature, power, …), `last_changed` / `last_updated`, any
state attribute other than `device_class`, icons, areas, labels, timestamps,
`platform`, disabled entities. A sensor reading changing a million times
changes the fingerprint zero times. This is the property that keeps
production from generating observations on state changes and is tested
directly (§10, T3; shadow criterion S4).

Consequence, accepted: renaming a device or entity changes the fingerprint,
because classifiers read names. That is a real input change.

### 7.2 Classifier set version

`CLASSIFIER_SET_VERSION` is an explicit constant. A test pins the SHA256 of
the classifier source files to it, so changing a classifier without bumping
the version fails the suite. A version bump legitimately yields one new input
per device and re-computed observations; resolved cases still do not reopen
(STEP 5) and human decisions are untouched (D0).

### 7.3 Sources — one gate

| Source | Trigger | Action |
|---|---|---|
| A — startup | add-on start | full sweep |
| A — daily | 24 h after the last completed sweep of any source | full sweep |
| existing | `device_registry_updated` | debounced full sweep |
| B | `entity_registry_updated` | debounced full sweep |

All four run the same gated sweep; events are debounced (bursts on
integration reload collapse into one sweep). A sweep costs three read
commands (device list, entity list, `get_states`) and in-memory
classification — a small read/compute cost; the fingerprint gate removes
redundant **writes**, so steady state writes only the `classification_sweeps`
row.

---

## 8. Shadow mode and activation

Add-on option `observation_mode`: `shadow` (**default**) | `active`.

- **shadow:** every sweep (all four sources) computes inputs, fingerprints,
  outcomes and would-be observations, and writes **only** the
  `classification_sweeps` row. No `classification_inputs`, no
  `classification_observations`, no `review_cases`.
- **active:** full behaviour of §6.

**Where the per-device fingerprints live (shadow and active).** Every
`classification_sweeps` row is append-only and contains:

- `baseline_sweep_id` — the sweep whose fingerprints this sweep compared
  against (shadow: the most recent previous **shadow** sweep; `NULL` for the
  first one);
- `devices_json` — one entry per discovered device:
  `{device_id: {outcome, reason, fingerprint, baseline_fingerprint,
  matched_classifiers}}`. `fingerprint` is `null` only for `skipped` devices
  (no complete input exists); `matched_classifiers` lists the would-be (shadow)
  or written (active) matches.

Gate baseline per device:

| Mode | Baseline fingerprint for a device |
|---|---|
| shadow | its `fingerprint` in `devices_json` of the `baseline_sweep_id` row (absent or `null` → not `unchanged`, i.e. attempted) |
| active | its latest `classification_inputs.fingerprint` (none → attempted) |

Because each row stores both the fingerprint it computed and the baseline
fingerprint it compared against, every `unchanged` decision — and therefore
S4 — can be re-checked later from the stored rows, without re-running the
sweep. Size: ~160 entries ≈ tens of KB per sweep.

Nothing is lost while in shadow: a registry change during shadow is caught
by the first active sweep, which covers all devices.

**Activation** is switching the option to `active`. It needs no new code,
no rebuild and no new deployment phase, but it is a **FULL GATE
(lightweight, single decision)**: the first active sweep starts writing
production data — one `classification_inputs` row per non-skipped device,
the observations for matching classifiers (probe expectation: ~20 matches on
~18 devices) and the resulting review cases. Before switching, S1–S6
(§10.2) are checked from the stored sweep rows and an explicit **GO** is
given; the check and the GO are recorded in the D2 attestation (or an
addendum to it).

---

## 9. Out of scope / later

- **STEP 7a** — as specified in `STEP7_ARCHITECTURE.md`; no amendment.
- **Behavioural evidence (C / C′)** — own ADR, after 7a has run. Preferred
  mechanism C′: read HA history / long-term statistics on demand and store
  only closed-window aggregates, rather than subscribing to `state_changed`.
  Never written to `classification_observations`. **Does not block 7a.**
- **Classifier-rule gaps** (§3.5) — separate future topic.
- **Per-entity `entity_registry/get` fallback** — only if shadow data
  requires it (§5.2).

---

## 10. Acceptance criteria

### 10.1 Implementation (tests, before D2)

- **T1 Input.** From a recorded HA payload: `domain` comes from `entity_id`;
  `device_class` from `get_states` attributes; a `binary_sensor` with
  `device_class: motion` is matched by `MotionSensorClassifier`; disabled
  entities are excluded and counted.
- **T2 Probe parity.** Classifying the probe's recorded payload with the new
  adapter reproduces the probe's corrected result: 18 classified, 130
  `no_match`, 20 matches, 4 changed.
- **T3 State-value independence.** Same structure, different state values /
  `last_changed` / other attributes → identical fingerprint; a different
  `device_class`, `entity_id`, name, manufacturer, model or classifier set
  version → different fingerprint.
- **T4 Missing metadata.** Enabled entity without a `get_states` entry →
  device `skipped/missing_metadata`, not `no_match`, no input row, gate not
  advanced; next sweep with the state present classifies it.
- **T5 Gate.** Second sweep without structural change: 0 new inputs, 0 new
  observations, 0 review-case changes.
- **T6 Coverage invariant.** Counts sum to `discovered`; every device has
  exactly one outcome per sweep.
- **T7 no_match.** Writes an input row with `outcome = no_match` and no
  observation.
- **T8 Errors.** A raising classifier → `error`, no input row, retried next
  sweep; other devices unaffected.
- **T9 Shadow.** In `shadow` mode no rows in `classification_inputs`,
  `classification_observations`, `review_cases`; exactly one
  `classification_sweeps` row per sweep, whose `devices_json` holds every
  device's outcome and fingerprint, and whose `baseline_sweep_id` points to
  the previous shadow sweep.
- **T10 Version pin.** Changing classifier source without bumping
  `CLASSIFIER_SET_VERSION` fails the suite.
- **T11 Immutability.** `classification_inputs` and `classification_sweeps`
  are append-only; `classification_observations.input_id` is protected by a
  new trigger (existing 13 D1 triggers untouched); existing rows keep
  `input_id = NULL` and cannot be backfilled.
- **T12 Offline replay.** An observation written after 7P re-classified from
  its stored input with the recorded version yields the same hypothesis.
- **T13 Regression.** All existing tests, `d0_verify.py` and `d1_verify.py`
  checks remain valid; a new `d2_verify.py` (new file, old verifiers not
  edited) covers the new triggers and tables.

### 10.2 Shadow in production (predefined — met ⇒ activate)

- **S1** Startup sweep completes; `discovered` equals the HA device count;
  coverage invariant holds.
- **S2** `error = 0`.
- **S3** Classified set matches Probe 7P's corrected result (18 devices, 20
  matches, 4 motion), except differences explained by registry changes since
  the probe.
- **S4** *(amended 2026-10-03, see below)* A continuous window of **at
  least 20 h** between two shadow sweeps, of any source, in which **every**
  fingerprinted device keeps an identical fingerprint in **every** sweep of
  the window and is gated `unchanged` (thousands of state changes apart).
  Registry changes outside that window (devices added, removed, or with a
  changed fingerprint) do not invalidate S4, but each must be re-evaluated
  without error and every stored gate decision must be consistent:
  `unchanged` only where `fingerprint` equals `baseline_fingerprint`, a
  re-evaluation only where it differs, and `baseline_fingerprint` equal to
  what the baseline sweep stored (§8). Devices whose fingerprint changed
  more than once are reported as a diagnostic, not a failure.
- **S5** `skipped/missing_metadata` ≤ 5 % of devices with entities, each
  listed; above that → decide on the §5.2 fallback before activation.
- **S6** Row counts of `classification_observations`, `review_cases`,
  human-decision columns and `classification_inputs` unchanged during shadow.

**S4 amendment (2026-10-03).** The original wording required a `daily`
sweep whose baseline was at least 20 h older. The daily sweep runs 24 h
after the last sweep of *any* source, and its baseline is always the
previous sweep, so on an instance whose registry emits events every few
hours the criterion could never be met although the gate worked: production
shadow data showed 25 sweeps in 60 h, none `daily`, longest gap 14.5 h. The
property S4 exists to prove — fingerprints do not move while states change —
is measured directly by the window above and does not depend on the sweep
source. The standard is not lowered: the window must be continuous, cover
every device, and every gate decision in the whole period is re-checked.
S3 is tightened at the same time: the *effective* result (each device's
latest evaluation) must match the probe too, not only the startup sweep.

If S1–S6 hold: explicit GO, then `observation_mode: active` (activation,
lightweight FULL GATE, §8). If not: fix,
redeploy through the normal gate, shadow again.

---

## 11. Impact on existing invariants

| Invariant | Effect |
|---|---|
| D0 human decisions immutable | none |
| D1 raw hypothesis + identity immutable | extended: `input_id` protected by a new trigger; new tables append-only; the 13 existing triggers unchanged |
| STEP 5 review cases | unchanged semantics; one expected burst at activation |
| M0/D0/D1 verifiers | kept as evidence tools; `d2_verify.py` added |

---

## 12. Delivery sequence

| Step | Gate | Reason |
|---|---|---|
| Accept this ADR, commit | — | document only |
| 7P implementation as one package (§5–§8, tests T1–T13) | **FAST-TRACK SAFE** | covered by this ADR; no production change; full test suite |
| D2 deployment, `observation_mode: shadow` | **FULL GATE** | schema change in production (new tables, column, triggers): package from git objects, SHA256 chain, backup, rebuild, `d2_verify.py` with negative control, attestation doc, tag after doc commit |
| Shadow evaluation S1–S6 | read-only | predefined criteria, no extra phase |
| Activation (`observation_mode: active`) | **FULL GATE** — lightweight, single decision | no code or rebuild, but the first active sweep writes inputs, observations and review cases; explicit GO after S1–S6 |
| STEP 7a | per its ADR | unchanged |
