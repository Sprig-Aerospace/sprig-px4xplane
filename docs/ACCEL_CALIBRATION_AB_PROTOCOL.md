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
- Current installed `config.ini`.
- PX4 diagnostics at t0, t5, and t30 after PX4 connects.
- Any launcher diagnostic report, if available.

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
5. Capture PX4 diagnostics at t0, t5, and t30.
6. Let the run continue until at least one `[ACCEL_CAL] COMPLETE` or enough `[ACCEL_S5_FINAL]` lines prove calibration did not complete.
7. Save the X-Plane log and diagnostic report as `baseline`.

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

For each variant, retain post-session-boundary log excerpts containing:

```text
[ACCEL_CAL]
[ACCEL_S1_RAW]
[ACCEL_S5_FINAL]
[ACCEL_OK]
[ACCEL_ERROR]
[TIMESTAMP_SUMMARY]
[RATE]
[DIAG_FLIGHTLOOP]
[TRANSPORT_EVENT]
```

Record whether `[ACCEL_CAL] COMPLETE` appeared, the reported scale factor, and whether `[ACCEL_S5_FINAL]` reports Z near `-9.81 m/s^2` during the first seconds after connect.

## Scoring

Use this table for the PR/issue verdict:

| Observation | Interpretation | Verdict |
|---|---|---|
| Baseline shows `Accel #0 fail` or validator failsafe YES at t5, while Run A or Run B is clean at t5/t30 | Calibration path poisons validator during startup | POISONING CONFIRMED |
| Baseline, Run A, and Run B all show the same validator failure | Failure is upstream of calibration, such as raw values, sign, timing, or cadence | POISONING DISPROVED for this symptom |
| Cross-session session 2 inherits a prior bad scale factor and validator remains failed | Preserved calibration state is harmful across reconnects | CROSS-SESSION POISONING CONFIRMED |
| Cross-session session 2 recalibrates or validator clears while level | Preserved state was not harmful in this scenario | CROSS-SESSION POISONING DISPROVED |
| Baseline `[ACCEL_S5_FINAL]` Z is not near `-9.81`, but Run B is near `-9.81` | Calibration path has a value-poisoning signal | VALUE-POISONING SIGNAL |

## Hand-Off Package

Attach or paste:

- The installed config for every variant.
- X-Plane log excerpts filtered to the current session boundary.
- t0/t5/t30 PX4 captures for every variant.
- A short verdict using the scoring table.

Keep issue #16 open after attaching the evidence so reviewers can compare the four variants.
