# D — Accelerometer Auto-Calibration as a First-Sample Poisoning Path

**Issue:** #9 · **Runner:** Kimi · **Cross-review:** Claude · **Status:** review artifact
**Scope:** calibration state-lifecycle and value-plausibility REVIEW only. No tuning, no value
changes, no production patches. Spec/protocol/artifact, not a fix.

---

## 0. TL;DR

`AccelCalibration` keeps **all** of its working state in function-scope/file-scope `static`
variables. The intended reset path (`AccelCalibration::reset()`) is only invoked from
`MAVLinkManager::reset(bool resetCalibration)` when that argument is `true`. In the two
production reconnect/accept call sites the argument is **`false`**, and the explicit inline
comment says calibration is deliberately preserved across sessions
(`ConnectionManager.cpp:540-544`). Therefore:

- A completed calibration (`calibrated_ = true`, `gravityScaleFactor_ != 1.0`) absolutely survives
  a disconnect → reconnect cycle.
- A partially collected calibration (`calibrationSamples_`, `stationaryCount_`, `sampleCount_`)
  also **survives** because `AccelCalibration::reset()` is never called on accept/disconnect; the
  auto-calibrate motion branch can clear them later, but there is no explicit session-scoped
  cleanup.
- During the first ~0–5 s after connect (at 60 FPS; shorter at higher FPS, longer with motion or
  low FPS) the plugin sends **uncalibrated** accel data (scale factor = 1.0, auto-calibration
  collecting samples) while PX4's sensor validator is bootstrapping.
- That window can latch a validator failsafe; because PX4 console output is captured as a single
  undated paste (Issue A evidence contract), a latched-then-cleared failsafe is
  indistinguishable from a persistent one.

**Verdict:** cross-session poisoning of the *calibration result* is **POSSIBLE by design**
(deliberately preserved), but only from a **completed prior session**; partial sample carry-over is
**POSSIBLE by omission** (no reset at all on the accept path). Whether it is actually causing the
reported `Accel #0 fail: STALE/TIMEOUT` requires the A/B diagnostic below.

---

## 1. AccelCalibration state-lifecycle diagram

All `AccelCalibration` state is `static` (`AccelCalibration.cpp:19-28`,
`AccelCalibration.h:99-110`):

| Static field | Initial value | Reset in `reset()` | Survives `MAVLinkManager::reset(false)`? | Notes |
|---|---|---|---|---|
| `initialized_` | `false` | no (set only in `initialize()`) | **YES** | `applyCalibration()` lazily calls `initialize()` if false; `reset()` does not clear it (`:45-57`). |
| `calibrated_` | `false` | **YES** → `false` | **YES** | Set `true` only in `performCalibration()` (`:217`). Not reset by `reset(false)`. |
| `autoCalibrate_` | `true` | no | **YES** | Loaded from `ConfigManager::accel_auto_calibrate` in `initialize()` (`:35`). |
| `gravityOffset_` | `Zero()` | **YES** → `Zero()` | **YES** | Legacy field, unused in v2 scaling. |
| `manualOffset_` | `Zero()` | no | **YES** | Loaded from config in `initialize()`; `reset()` never touches it. |
| `measuredGravityMagnitude_` | `0.0f` | **YES** → `0.0f` | **YES** | Not reset by `reset(false)`. |
| `gravityScaleFactor_` | `1.0f` | **YES** → `1.0f` | **YES** | The active correction multiplier. Not reset by `reset(false)`. |
| `calibrationSamples_` | empty | **YES** → `clear()` | **YES** | Leaks if not reset. |
| `stationaryCount_` | `0` | **YES** → `0` | **YES** | Leaks if not reset. |
| `sampleCount_` | `0` | **YES** → `0` | **YES** | Leaks if not reset. |

### Call graph across connect → run → disconnect → reconnect

```text
XPluginStart
  └── AccelCalibration::initialize()
        ├── sets initialized_ = true
        ├── calls reset()  → clears calibrated_, gravityScaleFactor_=1.0, samples, counters
        └── loads autoCalibrate_ and manualOffset_ from config.ini

First PX4 client accept
  └── ConnectionManager::tryAcceptConnection()
        ├── generation += 1                              [ConnectionManager.cpp:526]
        ├── emit "client_connected"                      [ConnectionManager.cpp:538]
        ├── resetFlightLoopTimers()
        ├── DataRefManager::resetActuatorValues()
        ├── MAVLinkManager::reset(false)                  [ConnectionManager.cpp:548]
        │     ├── g_sessionResetGeneration++              [MAVLinkManager.cpp:983]
        │     ├── hilActuatorControlsData = {}
        │     ├── TimestampProvider::reset()
        │     └── if (resetCalibration) AccelCalibration::reset();
        │           └── NOT TAKEN because resetCalibration == false
        └── emit "session_reset"                          [ConnectionManager.cpp:550]

Run phase (every flight loop while connected)
  └── MAVLinkManager::sendHILSensor()
        └── setAccelerationData()
              └── computeAcceleration()
                    ├── reads raw gload, converts to m/s²   [MAVLinkManager.cpp:151-153]
                    ├── AccelCalibration::applyCalibration(raw_accel)
                    │     ├── if not initialized_ → initialize() (first use only)
                    │     ├── if autoCalibrate_ && !calibrated_:
                    │     │     wait STATIONARY_WAIT_SAMPLES (50) stationary frames
                    │     │     then collect up to CALIBRATION_SAMPLES (200)
                    │     │     then performCalibration() → sets gravityScaleFactor_
                    │     ├── if calibrated_:  calibrated *= gravityScaleFactor_
                    │     └── calibrated -= manualOffset_
                    └── returns filtered_accel

Disconnect / reconnect
  └── ConnectionManager::disconnect()
  │       └── if hadClient:
  │             ├── DataRefManager::resetActuatorValues()
  │             ├── MAVLinkManager::reset()  // default resetCalibration = false
  │             │     └── AccelCalibration::reset() NOT called  [ConnectionManager.cpp:658]
  │             └── DataRefManager::disableOverride()
  │
  └── Later: new client accept
        └── ConnectionManager::tryAcceptConnection()
              └── MAVLinkManager::reset(false)  // again preserves calibration
                    └── AccelCalibration::reset() NOT called  [ConnectionManager.cpp:548]
```

### Key code snippets

`AccelCalibration::reset()` (`src/AccelCalibration.cpp:45-57`):

```cpp
void AccelCalibration::reset() {
    calibrated_ = false;
    gravityOffset_ = Eigen::Vector3f::Zero();
    measuredGravityMagnitude_ = 0.0f;
    gravityScaleFactor_ = 1.0f;
    calibrationSamples_.clear();
    stationaryCount_ = 0;
    sampleCount_ = 0;
    ...
}
```

`MAVLinkManager::reset(bool resetCalibration)` (`src/MAVLinkManager.cpp:978-996`):

```cpp
void MAVLinkManager::reset(bool resetCalibration) {
    hilActuatorControlsData = {};
    g_sessionResetGeneration++;
    ...
    if (resetCalibration) {
        AccelCalibration::reset();
    }
}
```

Client-accept path deliberately preserving calibration (`src/ConnectionManager.cpp:540-548`):

```cpp
// A newly accepted TCP socket is a new PX4 simulator session. Reset all
// session-scoped timing, parser, sequence, and actuator state before the
// flight loop sends the first frame. Keep accelerometer calibration across
// reconnects; recalibrating during PX4 startup can delay EKF accel-bias
// convergence and block arming.
...
MAVLinkManager::reset(false);
```

`handleClientDisconnect` also preserves calibration (`src/ConnectionManager.cpp:695-700`):

```cpp
if (resetAircraftState && hadClient) {
    ...
    DataRefManager::resetActuatorValues();
    MAVLinkManager::reset(false);   // default false again
    DataRefManager::disableOverride();
}
```

`ConnectionManager::disconnect()` (`src/ConnectionManager.cpp:645-660`) calls
`MAVLinkManager::reset()` with the default argument, which is also `false`
(`include/MAVLinkManager.h:23`).

---

## 2. Answers to the seven exact questions

### Q1. Does px4xplane send uncalibrated accel data for roughly the first ~5 seconds after connect? Quantify the window.

**Yes, it sends unscaled data for at least the calibration acquisition window.**

- `applyCalibration()` only scales when `calibrated_ == true`
  (`src/AccelCalibration.cpp:93-96`).
- Before calibration, `gravityScaleFactor_ = 1.0`, so the raw X-Plane gload value is forwarded
  (minus `manualOffset_`).
- To enter calibration:
  1. `isStationary()` requires `groundspeed < STATIONARY_THRESHOLD` (0.5 m/s)
     (`src/AccelCalibration.h:95`, `:131-133`).
  2. Then `stationaryCount_` must reach `STATIONARY_WAIT_SAMPLES` = 50
     (`src/AccelCalibration.h:96`, `:74-76`).
  3. Then `sampleCount_` must reach `CALIBRATION_SAMPLES` = 200 (`src/AccelCalibration.h:94`,
     `:135-150`).
- The flight-loop sensor rate is capped by X-Plane FPS. At 60 FPS the fastest realistic path is
  `50 + 200 = 250 frames ≈ 4.2 s` before the first scaled sample. At 30 FPS it is `≈ 8.3 s`.
- If the aircraft is not perfectly stationary on startup (e.g. wind, ground bump, VTOL spool-up),
  `stationaryCount_` is zeroed (`src/AccelCalibration.cpp:78-85`) and the window extends
  indefinitely until stable ground contact.

So the **uncalibrated send window is roughly 0–5 s at 60 FPS, longer at lower FPS or with motion**.

### Q2. Can a validator failsafe LATCH during that window and persist after data normalizes?

**Yes, that is structurally possible on the PX4 side.**

The plugin cannot tell whether a failsafe latched and cleared, because:

- The diagnostic bundle copies `px4_output.txt` as a single undated paste
  (`scripts/hitl_diagnostic_bundle.py:335`, per Issue A).
- PX4's sensor validator can flag `Accel #0 fail: STALE/TIMEOUT` during the startup window; once the
  validator votes a sensor unavailable it may keep the failure latched in `px4-sensors status` or
  `sensors status` output even after new healthy samples arrive.
- Without a `t0 / t5 / t30` capture (Issue A §7), a validator "YES" at one moment cannot be
  distinguished from a "YES" that is still active.

Therefore a transient calibration-poisoning event in the first seconds **can** leave a
historical/latched failsafe signature that looks identical to a persistent sensor fault.

### Q3. Does AccelCalibration preserve static state across reconnects? Is bad calibration state carryable across sessions?

**Yes to both.**

Every production reconnect/accept path calls `MAVLinkManager::reset(false)`, so
`AccelCalibration::reset()` is **not** invoked. The surviving fields are:

- `calibrated_`
- `gravityScaleFactor_` (the active correction)
- `measuredGravityMagnitude_`
- `manualOffset_`
- `autoCalibrate_`
- `initialized_`

If a previous session computed a bad scale factor (e.g. aircraft was moving during calibration,
free-fall, or a NaN/infinite path), that bad factor is applied to the next session's data from the
very first frame.

The fields that are **not** reset by `reset(false)` but **are** reset by `AccelCalibration::reset()`
include `calibrationSamples_`, `stationaryCount_`, and `sampleCount_`; these are currently cleared
only by `reset()` or by motion during collection, so on a clean disconnect they can also leak into
a reconnect until `initialize()` or `applyCalibration()` happens to clear them through the motion
branch.

### Q4. Verify whether `AccelCalibration::reset()` is called on every PX4 client accept/reconnect, and whether `MAVLinkManager::reset(false)` can carry stale calibration state into the next session.

**`AccelCalibration::reset()` is NOT called on every accept/reconnect.**

| Path | File:line | `MAVLinkManager::reset(...)` argument | `AccelCalibration::reset()` called? |
|---|---|---|---|
| New client accept | `ConnectionManager.cpp:548` | `false` | **NO** |
| Manual disconnect (had client) | `ConnectionManager.cpp:658` | default `false` | **NO** |
| Client disconnect with aircraft-state reset | `ConnectionManager.cpp:699` | `false` | **NO** |
| `AccelCalibration::initialize()` | `AccelCalibration.cpp:32` | n/a | **YES**, but only once at plugin startup |

The only way `AccelCalibration::reset()` is triggered is if someone explicitly calls it, or if
`MAVLinkManager::reset(true)` is invoked. No production path does the latter.

**Stale calibration state is therefore carried into the next session by design.** The inline
comment at `ConnectionManager.cpp:540-544` explicitly states the intent: *"Keep accelerometer
calibration across reconnects"*. Whether that design is safe is the question the A/B protocol
must answer.

### Q5. Immediately after connect, are accel values near [0,0,-9.81] and gyro near [0,0,0]? Where would we read this honestly?

**Only after applying Issue A's session-boundary rule.**

- The honest read requires evidence emitted **after** the latest `client_connected` +
  `session_reset` pair for the current `generation`
  (`ConnectionManager.cpp:526,538,550`, Issue A §8.1).
- In that post-boundary window, `computeAcceleration()` returns values derived from
  `sim/flightmodel/forces/g_axil`, `g_side`, `g_nrml` multiplied by `DataRefManager::g_earth` and
  `-1` (`MAVLinkManager.cpp:151-153`), then passed through `applyCalibration()`.
- For a stationary, level aircraft on the ground, the expected FRD vector is approximately
  `[0, 0, -9.81] m/s²` (PX4 FRD convention: gravity reaction points up, so Z is negative).
  The code itself asserts this at `MAVLinkManager.cpp:320-322`.
- Gyro is read directly from `sim/flightmodel/position/Prad/Qrad/Rrad` plus Gaussian noise
  (`MAVLinkManager.cpp:1088-1095`). A stationary aircraft should report near `[0,0,0] rad/s`.

The **existing** accel pipeline log line at `MAVLinkManager.cpp:299-304` (`[ACCEL_S5_FINAL]`) is the
single best source, but it is sampled only every 500 calls when
`debug_log_accel_pipeline = true` (`config/config.ini:51`). To read honestly one must:

1. Enable `debug_log_accel_pipeline = true` and `debug_log_sensor_values = true` in `config.ini`
   (`:39-52`).
2. Capture X-Plane `Log.txt`.
3. Filter the log to lines after the **last** `"event":"session_reset"` with the highest
   `"generation"`.
4. Look at `[ACCEL_S5_FINAL]` and `[ACCEL_OK]` / `[ACCEL_ERROR]` lines in the first 0–5 s.

### Q6. Could STALE/TIMEOUT be a VALUE problem rather than a timing problem? How to tell them apart?

**Yes. PX4's sensor validator uses both value and timing checks, and the plugin can emit both
pathologies.**

Possible value problems in this code path:

1. **Magnitude error before calibration.** Raw X-Plane gload may report `g_nrml != 1.0`
   (the original bug the v2 scaler was built for). An unscaled Z of `-10.3` or `-9.3` m/s² for
   multiple seconds is a sustained bias that PX4's validator can flag.
2. **NaN / inf from division.** `performCalibration()` divides by `measuredGravityMagnitude_`
   (`src/AccelCalibration.cpp:200`). It guards against `< 0.1f` (`:195-198`), but does not guard
   against zero or NaN explicitly. A zero input would be caught by the `< 0.1f` check; NaN would
   propagate through the comparison and could produce NaN scale factor, which then poisons every
   subsequent sample.
3. **Frozen cache.** `computeAcceleration()` caches by `currentTime == lastAccelUpdateTime`
   (`MAVLinkManager.cpp:138-140`). The cache is reset on generation change (`:111-119`), but if
   timestamps stall the same sample can repeat.
4. **Sign flip / wrong frame.** The `-1` factor is asserted as correct in comments, but if an
   aircraft model or X-Plane version changes sign convention, the resulting `[0,0,+9.81]` vector
   triggers the explicit `wrongSign` log at `MAVLinkManager.cpp:322`.

How to distinguish value from timing:

- **Timing:** `[TRANSPORT_EVENT]` backpressure/retry events; dt histogram from
  `TimestampProvider`; `last_outbound age_ms` growing.
- **Value:** `[ACCEL_S1_RAW]` vs `[ACCEL_S5_FINAL]` log lines; `measuredGravityMagnitude_` log
  from `performCalibration()`; PX4 `sensors status` reported accel values; whether the aircraft
  was stationary when calibration completed.

The A/B protocol below is designed to separate these by holding timing constant and varying only
the calibration path.

### Q7. What A/B run would prove/disprove poisoning — as a DIAGNOSTIC TOGGLE, explicitly not as a fix?

See §3. The toggle uses **only** existing config flags:

- `accel_auto_calibrate = true` / `false` (`config/config.ini:108`)
- `debug_accel_bypass_calibration = true` / `false` (`config/config.ini:52`)
- `debug_log_accel_pipeline = true` (`config/config.ini:51`)

---

## 3. Verdict: is cross-session poisoning possible?

**POSSIBLE by design, not yet proven to be happening.**

Code evidence:

1. All `AccelCalibration` working state is static and survives across the production
   `MAVLinkManager::reset(false)` calls that follow every accept/disconnect
   (`ConnectionManager.cpp:548,658,699`; `MAVLinkManager.cpp:994-996`).
2. The client-accept path explicitly documents the intent to preserve calibration across
   reconnects (`ConnectionManager.cpp:540-544`).
3. The first ~0–5 s (at 60 FPS, longer at lower FPS or with motion) of every session send
   unscaled accel data because `calibrated_` is `false` until `CALIBRATION_SAMPLES` = 200 samples
   are collected after `STATIONARY_WAIT_SAMPLES` = 50 stationary frames
   (`AccelCalibration.h:94-96`).
4. A prior session can leave `gravityScaleFactor_` and `calibrated_` set to values from a different
   aircraft attitude or model, which then apply immediately on reconnect.
5. The diagnostic bundle cannot currently distinguish a latched/historical validator failsafe from
   a current one (Issue A).

Whether this *actually* causes the observed `Accel #0 fail: STALE/TIMEOUT` requires measurement,
not code reading. The protocol below provides that measurement without changing any calibration
value, noise, offset, or PX4 parameter.

---

## 4. A/B diagnostic protocol (existing toggles only, measurement not fix)

**Goal:** determine whether first-seconds accel calibration state poisons PX4's sensor validator.

**Prerequisite:** honor Issue A's evidence contract. For every run, capture X-Plane `Log.txt` and
PX4 output, then analyze **only** lines after the latest `generation`'s
`"event":"session_reset"` / `"cause":"client_accept_reset"`.

### 4.1 Baseline run — auto-calibration ON (current default)

```ini
; config.ini
accel_auto_calibrate = true
accel_offset_x = 0.0
accel_offset_y = 0.0
accel_offset_z = 0.0
debug_accel_bypass_calibration = false
debug_log_accel_pipeline = true
debug_log_sensor_values = true
```

Procedure:

1. Start X-Plane, load aircraft, ensure aircraft is **stationary and level on the ground**.
2. Start PX4 SITL and let it connect.
3. Wait 60 s without arming/moving.
4. Capture PX4 output at `t0` (connect), `t5`, `t30` (Issue A §7).
5. Save `Log.txt`.

Signals to record:

- `[ACCEL_S5_FINAL]` values in first 0–5 s.
- `px4xplane: [ACCEL_CAL] COMPLETE ... Scale Factor: X` (or absence of completion).
- PX4 `sensors status` / `ekf2 status` accel/gyro validator lines at t0/t5/t30.
- Any `Accel #0 fail: STALE/TIMEOUT` or `High Accelerometer Bias` text.
- Whether arming is possible at t30.

### 4.2 Diagnostic run A — auto-calibration OFF, no bypass

```ini
; config.ini
accel_auto_calibrate = false
accel_offset_x = 0.0
accel_offset_y = 0.0
accel_offset_z = 0.0
debug_accel_bypass_calibration = false
debug_log_accel_pipeline = true
debug_log_sensor_values = true
```

Procedure identical to baseline.

This removes the calibration collection window entirely. `applyCalibration()` will skip the
`autoCalibrate_ && !calibrated_` branch (`src/AccelCalibration.cpp:70-86`) and apply only
`manualOffset_` (zero). The accel values sent immediately after connect should be raw X-Plane gload
× g_earth × −1.

### 4.3 Diagnostic run B — calibration bypassed entirely

```ini
; config.ini
accel_auto_calibrate = true   ; keep default
debug_accel_bypass_calibration = true
debug_log_accel_pipeline = true
debug_log_sensor_values = true
```

Procedure identical to baseline.

This short-circuits `applyCalibration()` at the top (`src/AccelCalibration.cpp:61-63`) and returns
raw data unchanged. It tests whether *any* calibration math (auto or manual) is implicated.

### 4.4 Cross-session poisoning test — reconnect with bad prior calibration

1. Run baseline (auto ON) with aircraft **deliberately moving/not level** during the first 10 s so
   calibration samples are collected in a bad attitude (or never complete).
2. Disconnect PX4 (`make px4_sitl` stop or menu disconnect).
3. Return aircraft to stationary/level.
4. Reconnect PX4 without restarting X-Plane.
5. Observe whether the second session inherits `calibrated_ = true` with a bad
   `gravityScaleFactor_` from the first session (look for `[ACCEL_CAL] COMPLETE` not re-appearing
   and `[ACCEL_S5_FINAL]` values immediately scaled by the old factor).

### 4.5 Pass/fail criteria

| Observation | Interpretation | Pass/fail for poisoning hypothesis |
|---|---|---|
| Baseline shows `Accel #0 fail` / validator YES at t5, but A and/or B show clean validator at t5/t30 | Calibration path poisons validator during startup window | **POISONING CONFIRMED** |
| Baseline, A, and B all show the same validator failure | Problem is upstream of calibration (raw value, sign, timing, cadence) | **POISONING DISPROVED for this symptom** |
| Reconnect inherits old scale factor and validator stays failed | Cross-session state carry-over is a real poisoning vector | **CROSS-SESSION POISONING CONFIRMED** |
| Reconnect re-calibrates cleanly and validator clears | Deliberate preservation is safe in this scenario | **CROSS-SESSION POISONING DISPROVED** |
| `[ACCEL_S5_FINAL]` Z is not near −9.81 in first seconds in baseline but is near −9.81 in B | Calibration is delaying correct gravity report | **VALUE-POISONING SIGNAL** |

### 4.6 What this protocol does NOT do

- It does **not** change calibration offsets, noise, or sensor values.
- It does **not** tune `CAL_*` / `EKF2_*` parameters.
- It does **not** add catch-up / multi-send / async publishing.
- It is a **measurement** to hand to Issue E; any recommendation to change the default
  `reset(false)` behavior must come from the result of this protocol, not from this artifact.

---

## 5. Hand-off to Issue E

Issue E should receive:

1. This artifact's §4 A/B results (three config variants + reconnect test).
2. The filtered post-boundary `[ACCEL_S*]` log excerpts.
3. The PX4 `sensors status` captures at t0/t5/t30 for each variant.
4. A clear pass/fail statement against the table in §4.5.

If poisoning is confirmed, the *implementation* decision (e.g. whether to call
`MAVLinkManager::reset(true)` on accept, or to reset only on aircraft/model change) is out of scope
for this review and must be planned separately.

---

## 6. Forbidden-change boundary (self-audit)

This artifact is spec only. No code changed; no calibration value, offset, noise, or sensor value
modified; no `CAL_*` / `EKF2_*` parameter tuned; no catch-up/multi-send/async publishing proposed.
The A/B toggle uses existing config flags and is explicitly labeled diagnostic-only, not a
recommended fix. ✔

---

## 7. Cross-review sign-off (Claude)

Cross-reviewer: Claude (code-evidence verification + forbidden-change-boundary enforcement, the
role Issue #9 assigns me — "ensure the A/B protocol stays a measurement toggle, never a
recommended fix").

### (1) Assumptions challenged — every code citation independently verified

| Runner claim | Verdict | My verification |
|---|---|---|
| `AccelCalibration::reset()` clears `calibrated_`, `gravityScaleFactor_=1.0`, samples, counters; does NOT clear `initialized_`/`manualOffset_`/`autoCalibrate_` | **Confirmed** | `AccelCalibration.cpp:45-57` reads exactly as quoted; `initialized_`/`manualOffset_`/`autoCalibrate_` are absent from `reset()`. |
| `MAVLinkManager::reset(bool resetCalibration=false)` only calls `AccelCalibration::reset()` when arg is `true` | **Confirmed** | `include/MAVLinkManager.h:23` default `= false`; `MAVLinkManager.cpp:978-996` gates `AccelCalibration::reset()` behind `if (resetCalibration)`. |
| All three production paths pass `false` → reset never fires on accept/disconnect | **Confirmed** | `ConnectionManager.cpp:548` (`false`), `:658` (default → `false`), `:699` (`false`). No `reset(true)` anywhere in `src/`. |
| Constants: 50 stationary-wait, 200 samples, 0.5 m/s threshold | **Confirmed** | `AccelCalibration.h:94-96`. |
| Bypass flag short-circuits `applyCalibration()` at the top | **Confirmed** | `AccelCalibration.cpp:61-63` returns `raw_accel` before `initialize()`. |
| Config flags exist with cited defaults | **Confirmed** | `config.ini:108` (`accel_auto_calibrate=true`), `:52` (`debug_accel_bypass_calibration=false`), `:51` (`debug_log_accel_pipeline`), `:41` (`debug_log_sensor_values`). |
| Uncalibrated send window ≈ 0–5 s at 60 FPS (50+200 frames) | **Confirmed, with one nuance** | Arithmetic is right (`250 frames / 60 fps ≈ 4.2 s`). Nuance: `isStationary()` gates on **groundspeed < 0.5 m/s**, so a perfectly stationary aircraft does enter quickly, but the window is open from frame 1 regardless — see note (3). |

### (2) Forbidden-change boundary intact? **Y — and this was my primary review charge.**
The artifact changes no code, no calibration value/offset/noise, no `CAL_*`/`EKF2_*` param, and
proposes no catch-up/multi-send/async. The A/B protocol (§4) uses **only pre-existing config
toggles** (`accel_auto_calibrate`, `debug_accel_bypass_calibration`, `debug_log_*`) and is
explicitly fenced as diagnostic-only in §4.6 and §5, with the implementation decision deferred to a
separate ticket. **The toggle never slides into a recommended fix.** Confirmed clean.

### (3) Gaps / refinements the runner should note
- **Intra-session poisoning is the more probable trigger than cross-session — sharpen the
  emphasis.** The §0/§3 verdict leans on *cross-session* carry-over, but the observed
  `Accel #0 fail: STALE/TIMEOUT` most likely comes from a **first-ever connect** (no prior session
  to inherit from). The decisive vector is the **0–5 s uncalibrated window on a fresh boot**
  (gravityScaleFactor_=1.0, calibrated_=false), which the **baseline-vs-run-B** comparison in §4.1
  vs §4.3 already isolates. Recommend stating that baseline-vs-B is the primary test and the
  reconnect test (§4.4) is the secondary/confirmatory one.
- **Partial-sample carry-over is mostly inert when `calibrated_` survives `true`.** If a prior
  session completed (`calibrated_=true`), the reconnect skips the entire
  `autoCalibrate_ && !calibrated_` collection branch (`AccelCalibration.cpp:70`), so leftover
  `stationaryCount_`/`sampleCount_`/`calibrationSamples_` are never read — the live poison is the
  stale **`gravityScaleFactor_`**, not the stale counters. The counter-leak only bites if a prior
  session left `calibrated_=false` with a *partial* count, which then resumes mid-collection. Worth
  a one-line precision edit to §1/§3 so [DIAG] doesn't chase the inert counters.
- **NaN guard (Q6 #2) is a hypothesis, not yet verified to line.** The claim that
  `performCalibration()` guards `< 0.1f` but not NaN should be tagged "verify at the divide site"
  rather than asserted; I did not confirm the exact guard line in this pass. Low priority — it is
  listed as a *possible* value pathology, which is appropriate framing.
- **Run-A vs Run-B differ by `manualOffset_`, not just the collection window.** Run A (auto off)
  still subtracts `manualOffset_`; Run B (bypass) skips it. Here offsets are zero so the runs are
  equivalent, but the protocol should state "valid only while `accel_offset_*` = 0" so a future
  operator with nonzero offsets doesn't misread the A/B delta.

**Cross-review verdict: ACCEPT-WITH-NOTES** — every code citation is accurate and the A/B protocol
is correctly fenced as measurement-only; the only refinements are emphasis (intra-session window is
the likelier first-failure than cross-session carry) and two precision notes (inert counters vs
live scale factor; offset-zero precondition). None block handing this to Issue E.
