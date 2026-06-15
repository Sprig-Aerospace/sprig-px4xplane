# HITL Cadence Diagnostics

This is an evidence gate before scheduler changes. Do not use this workflow to claim the PX4/X-Plane HITL issue is fixed until a human run captures the required PX4 and X-Plane evidence.

## What This Captures

- resolved installed `config.ini` path from X-Plane `Log.txt`
- installed `config.ini` SHA-256 hash and diff against repo `config/config.ini`
- effective `config_name`
- effective `mavlink_sensor_rate_hz`, `mavlink_gps_rate_hz`, `mavlink_state_rate_hz`, and `mavlink_rc_rate_hz`
- `px4xplane` excerpts from X-Plane `Log.txt`
- versioned `[RATE]` HIL_SENSOR send-rate and `estimated_fps` lines with `generation`,
  `wall_time_usec`, count-over-wall-time `rate_hz`, and HIL_SENSOR dt p50/p95/max buckets;
  emitted **unconditionally** every 1000 HIL_SENSOR messages, not gated by
  `debug_log_sensor_timing`
- `[TIMESTAMP_SUMMARY]` drift/delta lines with `generation`, `wall_time_usec`, and
  wall-clock-referenced `drift_ms`; emitted **unconditionally** every 1000 sensor frames, not
  gated by `debug_log_sensor_timing`
- callback/FPS timing from structured `[TRANSPORT_EVENT]` lines
- transport/drop evidence including `send_backpressure`, `send_retry_limit`, `dropping this frame`, `send failure`, and `broken pipe`
- a `session_boundary.json` file identifying the current PX4 session boundary
- a `historical/` directory containing pre-boundary evidence excluded from current-readiness metrics
- launcher-generated `px4_t0.txt`, `px4_t5.txt`, `px4_t30.txt`, and `timed_capture_manifest.json`
  files, when provided
- exact PX4 commands for the operator to run and paste into the bundle notes

The bundle treats current-readiness evidence as lines at or after the latest `session_reset` for the highest `transport_generation`. Earlier lines are retained under `historical/` for forensics only.

Invariant: a `stale_client_replaced` transport event is **always** classified as historical, never current. A stale replacement records the teardown of a prior transport session and is pre-boundary by definition, so it is routed to `historical/` unconditionally — even if a malformed or hostile log places it after the current-session boundary line. (An unknown-`diag_version` stale event is not trusted and is recorded as a version mismatch rather than current evidence.)

## Enable Log Evidence

For the diagnostic run only, set this in the installed plugin config:

```ini
debug_log_sensor_timing = true
```

`debug_log_sensor_timing = true` enables the per-frame detailed sensor-timing/drift lines.
The summary `[RATE]` and `[TIMESTAMP_SUMMARY]` lines are emitted unconditionally every 1000
frames regardless of this flag (see above), so they are present even on a default config.

Do not change `mavlink_*_rate_hz`, PX4 params, TimestampProvider behavior, TCP behavior, or the HIL_SENSOR scheduler while collecting this evidence.

For the accel-calibration poisoning A/B diagnostic, use [ACCEL_CALIBRATION_AB_PROTOCOL.md](ACCEL_CALIBRATION_AB_PROTOCOL.md). That protocol changes only existing config toggles and requires a fresh PX4 process and a fresh X-Plane process for Baseline, Run A, and Run B.

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
  --px4-capture-dir "/path/to/px4-timed-diagnostics-latest"
```

The bundle is written under `build/diagnostics/hitl-cadence-*`.

## Timed PX4 Captures

The launcher starts the `t0` / `t5` / `t30` capture schedule automatically when PX4 is launched.
The `Collect T Diagnostics` action only prints the already-created files; it does not start or
reschedule the captures. Each file must carry in-band UTC metadata headers:

```text
# px4_capture_offset: t0
# px4_capture_offset_sec: 0
# px4_capture_scheduled_utc: 2026-06-15T22:15:03Z
# px4_capture_started_utc: 2026-06-15T22:15:03Z
# px4_capture_finished_utc: 2026-06-15T22:15:18Z
```

The bundle rejects offset captures without these headers, and rejects non-monotonic scheduled
times. File modification time is not authoritative.

If the launcher is unavailable, run the command set below at `t0`, `t5`, and `t30` from one
continuous PX4 session and add the same metadata headers manually before bundling.

## PX4 Commands

Run the full command set in the PX4 shell three times: immediately after PX4 connects (`t0`), five
seconds later (`t5`), and thirty seconds later (`t30`). Save the outputs as `px4_t0.txt`,
`px4_t5.txt`, and `px4_t30.txt`.

```text
param show IMU_INTEG_RATE
param show EKF2_PREDICT_US
param show EKF2_EN
param show SYS_MC_EST_GROUP
param show SYS_HAS_MAG
param show EKF2_MAG_TYPE
param show EKF2_MULTI_IMU
param show SENS_IMU_MODE
param show SENS_EN_GPSSIM
param show SENS_EN_BAROSIM
param show SENS_EN_MAGSIM
listener sensor_accel 10
listener sensor_gyro 10
listener sensor_baro 5
listener sensor_mag 5
listener sensor_gps 5
listener vehicle_imu 10
listener vehicle_imu_status 3
listener vehicle_acceleration 10
listener vehicle_angular_velocity 10
listener vehicle_attitude 5
listener estimator_selector_status 3
listener vehicle_air_data 10
listener vehicle_magnetometer 10
listener vehicle_gps_position 10
listener differential_pressure 3
listener airspeed_validated 3
listener estimator_status 5
listener estimator_status_flags 5
listener estimator_sensor_bias 5
listener vehicle_local_position 5
listener vehicle_global_position 5
ekf2 status
sensors status
px4-sensors status
commander status
commander check
mavlink status
work_queue status
uorb top
uorb top -1 sensor_baro
uorb top -1 vehicle_gps_position
uorb status
```

The bundle also writes `px4_command_sheet.md` with this read-only command list. Do not use
`uorb status <topic>`; PX4 expects `uorb top -1 <topic>` for topic-filtered single-shot rate views.
The legacy `--px4-output` paste is still accepted for compatibility, but any validator/failsafe
`YES` line without a `t0`/`t5`/`t30` offset is historical/non-decisive.

## Human Checklist

- installed plugin path
- installed `config.ini` hash/diff vs repo
- X-Plane render FPS mean/min, if available
- HIL_SENSOR count-over-wall-time rate and dt p50/p95/max buckets
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
