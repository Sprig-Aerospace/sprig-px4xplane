# Accel Calibration A/B Diagnostic Protocol

This protocol is for issue #16. It is a measurement run only. Do not use these toggles as a fix, and do not change PX4 params, scheduler cadence, transport behavior, sensor noise, calibration offsets, or airframe files while collecting this evidence.

## Purpose

Determine whether the first seconds of `AccelCalibration` state can poison PX4's accel/gyro validator, and whether calibration state survives reconnects in a harmful way.

Baseline, Run A, and Run B must each start from a fresh X-Plane process so plugin static state does not leak between variants. The cross-session test is the only variant that intentionally reconnects PX4 without restarting X-Plane.

## Common Setup

Use the installed plugin config:

```text
~/X-Plane 12/Resources/plugins/px4xplane/64/config.ini
```

Before every variant:

- Keep the aircraft stationary and level on the ground, except for the deliberate-motion phase of the cross-session test.
- Keep `accel_offset_x = 0.0`, `accel_offset_y = 0.0`, and `accel_offset_z = 0.0`.
- Set `debug_log_accel_pipeline = true` and `debug_log_sensor_values = true`.
- Do not change `mavlink_*_rate_hz`, PX4 params, sensor noise, calibration code, or scheduler behavior.
- Start from a clean X-Plane process for Baseline, Run A, and Run B.

Capture for each variant:

- X-Plane `Log.txt` after the run.
- Current installed `config.ini`, plus its SHA-256 hash, recorded per variant so the
  reviewer can confirm exactly which config produced each capture set.
- PX4 diagnostics at t0, t5, and t30 after PX4 connects.
- Elapsed seconds from PX4 connect to the first `[ACCEL_CAL] COMPLETE` (or `COMPLETE: none`
  plus the first-30 s `[ACCEL_S5_FINAL]` excerpt as proof it did not complete).
- X-Plane FPS for the variant, sampled while stationary.
- Proof the intended toggle is active: the `[ACCEL_S2_CAL]` line showing `Calibrated=` /
  `ScaleFactor=` for Baseline/Run A, or evidence the bypass path is taken for Run B
  (`debug_accel_bypass_calibration = true` in the hashed config and the corresponding
  `[ACCEL_S5_FINAL]` showing the raw, uncalibrated value).
- Any launcher diagnostic report, if available.

Controlled-run requirements that apply to every variant:

- Use a **fresh X-Plane process** for Baseline, Run A, and Run B (only the cross-session
  variant reconnects PX4 without restarting X-Plane, by design).
- The per-variant `config.ini` hash must be committed/attached with the evidence.
- Do not change cadence, PX4 params, sensor noise, offsets, or calibration code between runs.

## Variant 1: Baseline

Use the default auto-calibration path:

```ini
accel_auto_calibrate = true
accel_offset_x = 0.0
accel_offset_y = 0.0
accel_offset_z = 0.0
debug_accel_bypass_calibration = false
debug_log_accel_pipeline = true
debug_log_sensor_values = true
```

Procedure:

1. Quit X-Plane completely if it is running.
2. Apply the config block above.
3. Start X-Plane, load the aircraft, and keep it stationary and level.
4. Start PX4 and let it connect.
5. Capture PX4 diagnostics at t0, t5, and t30, and note the X-Plane FPS while stationary.
6. Let the run continue until at least one `[ACCEL_CAL] COMPLETE` appears, or until at
   least 30 s of `[ACCEL_S5_FINAL]` lines prove calibration did not complete. Record the
   elapsed seconds from PX4 connect to the first `[ACCEL_CAL] COMPLETE` (or `COMPLETE: none`).
7. Save the X-Plane log, the hashed `config.ini`, and diagnostic report as `baseline`.

## Variant 2: Run A, Auto-Calibration Off

This removes the auto-calibration collection window but still applies zero manual offsets.

```ini
accel_auto_calibrate = false
accel_offset_x = 0.0
accel_offset_y = 0.0
accel_offset_z = 0.0
debug_accel_bypass_calibration = false
debug_log_accel_pipeline = true
debug_log_sensor_values = true
```

Procedure is identical to Baseline, but save outputs as `run-a-auto-off`.

## Variant 3: Run B, Calibration Bypassed

This short-circuits `AccelCalibration::applyCalibration()` and sends the raw accel path through.

```ini
accel_auto_calibrate = true
accel_offset_x = 0.0
accel_offset_y = 0.0
accel_offset_z = 0.0
debug_accel_bypass_calibration = true
debug_log_accel_pipeline = true
debug_log_sensor_values = true
```

Procedure is identical to Baseline, but save outputs as `run-b-bypass`.

## Variant 4: Cross-Session Carry-Over

This is the only variant where PX4 reconnects without restarting X-Plane.

Use the Baseline config:

```ini
accel_auto_calibrate = true
accel_offset_x = 0.0
accel_offset_y = 0.0
accel_offset_z = 0.0
debug_accel_bypass_calibration = false
debug_log_accel_pipeline = true
debug_log_sensor_values = true
```

Procedure:

1. Quit X-Plane completely if it is running.
2. Apply the baseline config.
3. Start X-Plane and PX4.
4. During the first 10 seconds after PX4 connects, deliberately keep the aircraft moving or not level so calibration completes badly or does not complete.
5. Capture PX4 diagnostics at t0, t5, and t30 for session 1.
6. Stop PX4, but do not restart X-Plane.
7. Return the aircraft to stationary and level.
8. Start PX4 again and let it reconnect.
9. Capture PX4 diagnostics at t0, t5, and t30 for session 2.
10. Save outputs as `cross-session-session1` and `cross-session-session2`.

## PX4 Capture Timing

For each t0, t5, and t30 capture, collect the command set from issue #13. At minimum the reviewer needs:

```text
param show IMU_INTEG_RATE
listener vehicle_imu 10
listener vehicle_acceleration 10
listener vehicle_angular_velocity 10
listener estimator_status 5
listener estimator_status_flags 5
listener estimator_sensor_bias 5
listener vehicle_local_position 5
listener vehicle_global_position 5
sensors status
ekf2 status
commander check
uorb top
```

Label each paste or file with the variant, session if applicable, and offset:

```text
baseline-t0.txt
baseline-t5.txt
baseline-t30.txt
run-a-auto-off-t0.txt
run-a-auto-off-t5.txt
run-a-auto-off-t30.txt
run-b-bypass-t0.txt
run-b-bypass-t5.txt
run-b-bypass-t30.txt
cross-session-session1-t0.txt
cross-session-session1-t5.txt
cross-session-session1-t30.txt
cross-session-session2-t0.txt
cross-session-session2-t5.txt
cross-session-session2-t30.txt
```

## X-Plane Log Signals

### Accel tag inventory (verified against source)

Only the tags below are emitted by the plugin. They were confirmed by grepping
`src/` and `include/` on the protocol branch. Do not rely on any tag not listed
here as confirmed; if a tag is marked "NOT in source" the protocol must not treat
it as observable evidence.

| Tag | Source location | Confirmed? | Notes |
|---|---|---|---|
| `[ACCEL_S1_RAW]` | `src/MAVLinkManager.cpp:160` | YES | Raw accel after the -1 factor. |
| `[ACCEL_S2_CAL]` | `src/MAVLinkManager.cpp:182` | YES | After calibration; logs `ScaleFactor` and `Calibrated=`. |
| `[ACCEL_S3_VIB]` | `src/MAVLinkManager.cpp:245` | YES | After vibration noise (only when groundspeed > 0.5 m/s). |
| `[ACCEL_S4_BIAS]` | `src/MAVLinkManager.cpp:266` | YES | Bias stage; `bias=DISABLED(v3.3.2)`. |
| `[ACCEL_S5_FINAL]` | `src/MAVLinkManager.cpp:302` | YES | Final value sent to PX4. |
| `[ACCEL_CAL]` | `src/AccelCalibration.cpp:238` | YES | Calibration status line; `[ACCEL_CAL] COMPLETE (v2 SCALING) ...` on success. |
| `[ACCEL_OK]` | — | NO (literal) | Not a literal source tag; it only appears as a runtime expansion of the `[ACCEL_%s]` format at `src/MAVLinkManager.cpp:328` where `status` is `"OK"` when the aircraft is stationary. Treat any `[ACCEL_OK]` line as the stationary branch of `[ACCEL_%s]`, not as a dedicated success tag. The protocol does not score on it. |
| `[ACCEL_ERROR]` | — | NO (literal) | Same as above: a runtime expansion of `[ACCEL_%s]` at `src/MAVLinkManager.cpp:328` where `status` is `"ERROR"` (set when `filtered_accel.z > 0` while stationary, i.e. a sign check). It is a sign-convention warning, not a calibration-failure tag. Not present as a literal in source; protocol cannot rely on it. |

For each variant, retain post-session-boundary log excerpts containing:

```text
[ACCEL_CAL]
[ACCEL_S1_RAW]
[ACCEL_S2_CAL]
[ACCEL_S3_VIB]
[ACCEL_S4_BIAS]
[ACCEL_S5_FINAL]
[TIMESTAMP_SUMMARY]
[RATE]
[DIAG_FLIGHTLOOP]
[TRANSPORT_EVENT]
```

### Gravity tolerance band

"Near -9.81" is defined as **Z in the band -9.81 ± 0.2 m/s² (i.e. -10.01 to -9.61 m/s²)**
on the body-frame Z axis while stationary and level. Basis: the plugin uses
`GRAVITY = 9.81` (`src/MAVLinkManager.cpp:31`) as expected gravity; at rest the only
applied noise is the interpolator measurement noise `ACCEL_MEAS_NOISE = 0.05 m/s²`
(`include/SensorInterpolator.h:135`), because vibration noise (σ = 0.1 m/s²,
`src/MAVLinkManager.cpp:42`) is only injected when groundspeed > 0.5 m/s. A ±0.2 m/s²
band is therefore ~4σ over the at-rest noise and tolerant of small attitude error.
Use this numeric band everywhere; do not record subjective "near -9.81" judgements.

### Per-run required anchors

For every variant (and every session of the cross-session variant) record, alongside the captures:

- **Calibration completion time:** elapsed wall-clock seconds from PX4 connect to the
  first `[ACCEL_CAL] COMPLETE` line. If no `COMPLETE` line appears, record `COMPLETE: none`
  and attach the `[ACCEL_S5_FINAL]` excerpt covering at least the first 30 s after connect
  as proof calibration did not complete.
- **Reported scale factor** from the `[ACCEL_CAL] COMPLETE` line (or `n/a` if none).
- **`[ACCEL_S5_FINAL]` Z band check:** whether Z is within -9.81 ± 0.2 m/s² during the
  first seconds after connect (record the actual min/max Z observed, not just yes/no).
- **X-Plane FPS** for the variant, sampled while stationary (X-Plane Data Output frame-rate
  field or the on-screen FPS counter). Record one value per variant: Baseline, Run A, Run B
  (and per session for cross-session). Runs whose FPS differs by more than ~20% from Baseline
  are not directly comparable and must be flagged.

## Scoring

The **primary signal is Baseline vs Run B (calibration bypassed)**. Run B isolates the
calibration transform itself: it shares Baseline's config except for
`debug_accel_bypass_calibration`, so a Baseline-vs-Run-B difference points directly at the
calibration math/state. Run A (auto-calibration off) is a **secondary/corroborating** signal
only — turning auto-cal off changes the collection window but still routes through the
calibration apply path, so a Run-A-only difference is weaker evidence and must not be used to
declare poisoning on its own.

Decision order:

1. Compare Baseline vs Run B first. A clean Run B with a failing Baseline (or a Z-band
   difference between them) is the confirming signal.
2. Use Run A only to corroborate the Baseline/Run B result, never as the sole basis.
3. Evaluate cross-session rows independently.

Use this table for the PR/issue verdict:

| Observation | Interpretation | Verdict |
|---|---|---|
| Baseline shows `Accel #0 fail` or validator failsafe YES at t5, while **Run B** is clean at t5/t30 | Calibration transform poisons validator during startup (primary signal) | POISONING CONFIRMED |
| Baseline fails but only **Run A** (not Run B) is clean | Weaker, collection-window-specific signal; corroborating only — do not declare poisoning on Run A alone | POISONING SUSPECTED (needs Run B) |
| Baseline and **Run B** show the same validator failure | Failure is upstream of the calibration transform, such as raw values, sign, timing, or cadence | POISONING DISPROVED for this symptom |
| Cross-session session 2 inherits a prior bad scale factor and validator remains failed | Preserved calibration state is harmful across reconnects | CROSS-SESSION POISONING CONFIRMED |
| Cross-session session 2 recalibrates or validator clears while level | Preserved state was not harmful in this scenario | CROSS-SESSION POISONING DISPROVED |
| Baseline `[ACCEL_S5_FINAL]` Z is outside -9.81 ± 0.2 m/s², but **Run B** Z is within -9.81 ± 0.2 m/s² | Calibration transform has a value-poisoning signal | VALUE-POISONING SIGNAL |

Every verdict must cite the per-variant FPS and the config hashes used, so the reviewer can
confirm the runs are comparable and that the intended toggle was active.

## Hand-Off Package

Attach or paste:

- The installed config for every variant.
- X-Plane log excerpts filtered to the current session boundary.
- t0/t5/t30 PX4 captures for every variant.
- A short verdict using the scoring table.

Keep issue #16 open after attaching the evidence so reviewers can compare the four variants.
