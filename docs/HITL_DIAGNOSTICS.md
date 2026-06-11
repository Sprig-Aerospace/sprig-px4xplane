# HITL Cadence Diagnostics

This is an evidence gate before scheduler changes. Do not use this workflow to claim the PX4/X-Plane HITL issue is fixed until a human run captures the required PX4 and X-Plane evidence.

## What This Captures

- resolved installed `config.ini` path from X-Plane `Log.txt`
- installed `config.ini` SHA-256 hash and diff against repo `config/config.ini`
- effective `config_name`
- effective `mavlink_sensor_rate_hz`, `mavlink_gps_rate_hz`, `mavlink_state_rate_hz`, and `mavlink_rc_rate_hz`
- `px4xplane` excerpts from X-Plane `Log.txt`
- `HIL_SENSOR` send-rate lines when `debug_log_sensor_timing = true`
- callback/FPS timing from structured `[TRANSPORT_EVENT]` lines
- TimestampProvider drift/delta lines when `debug_log_sensor_timing = true`
- transport/drop evidence including `send_backpressure`, `send_retry_limit`, `dropping this frame`, `send failure`, and `broken pipe`
- exact PX4 commands for the operator to run and paste into the bundle notes

## Enable Log Evidence

For the diagnostic run only, set this in the installed plugin config:

```ini
debug_log_sensor_timing = true
```

Do not change `mavlink_*_rate_hz`, PX4 params, TimestampProvider behavior, TCP behavior, or the HIL_SENSOR scheduler while collecting this evidence.

## Run The Bundle Script

After the live HITL run, collect the evidence:

```bash
python3 scripts/hitl_diagnostic_bundle.py
```

The default inputs are:

```text
~/X-Plane 12/Log.txt
~/X-Plane 12/Resources/plugins/px4xplane/64/config.ini
```

If the files are elsewhere:

```bash
python3 scripts/hitl_diagnostic_bundle.py \
  --xplane-log "/path/to/Log.txt" \
  --installed-config "/path/to/px4xplane/64/config.ini" \
  --px4-output "/path/to/px4-shell-output.txt"
```

The bundle is written under `build/diagnostics/hitl-cadence-*`.

## PX4 Commands

Run these in the PX4 shell during or immediately after the HITL attempt and paste the output into the issue or into a text file passed with `--px4-output`:

```text
param show IMU_INTEG_RATE
listener vehicle_imu 10
listener vehicle_acceleration 10
listener vehicle_angular_velocity 10
listener estimator_status 5
listener estimator_innovations 5
listener vehicle_global_position 5
uorb top
ekf2 status
```

## Human Checklist

- installed plugin path
- installed `config.ini` hash/diff vs repo
- X-Plane render FPS mean/min, if available
- whether X-Plane was paused, backgrounded, in menu, FPS-limited, or graphics-limited
- PX4 effective `IMU_INTEG_RATE` after clamp
- PX4 observed rates for `vehicle_imu`, `vehicle_acceleration`, and `vehicle_angular_velocity`
- accel and gyro validator status
- EKF2 update event count
- EKF2 `time_slip`
- timestamp, bias, and innovation warnings verbatim
- whether PX4 appears lockstep-sensitive, or `unknown` with evidence

## Decision Rules

- render FPS approximately callback Hz approximately HIL_SENSOR send Hz approximately PX4 IMU rate approximately 24: frame/callback-bound operation; do not assume scheduler fix first.
- callback Hz greater than HIL_SENSOR send Hz: scheduler gating/throttle issue.
- HIL_SENSOR send Hz greater than PX4 received Hz: transport/drop/backpressure issue.
- `time_usec` deltas distorted or drift from callback/wall time: timestamp-clock fix must precede cadence changes.
- installed config differs from repo config: fix packaging/install/config drift and rerun diagnostics.
- PX4 lockstep confirmed: catch-up scheduling is forbidden unless separately proven lockstep-safe.
- PX4 requires at least 50 Hz and the target machine cannot sustain at least 50 callback Hz: catch-up/async publishing may only be considered after a shadow-mode bias/timestamp test.
