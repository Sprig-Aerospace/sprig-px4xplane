# Accel Calibration A/B Diagnostic Protocol

This protocol is for issue #16. It is a measurement run only. Do not use these toggles as a fix, and do not change PX4 params, scheduler cadence, transport behavior, sensor noise, calibration offsets, or airframe files while collecting this evidence.

## Purpose

Determine whether the first seconds of `AccelCalibration` state can poison PX4's accel/gyro validator, and whether calibration state survives reconnects in a harmful way.

Baseline, Run A, and Run B must each start from a **fresh PX4 process and a fresh X-Plane process** so plugin static state does not leak between variants. The cross-session test is the only variant that intentionally reconnects PX4 without restarting X-Plane.

## Common Setup

Use the installed plugin config:

```text
~/X-Plane 12/Resources/plugins/px4xplane/64/config.ini
```

Before every variant:

- Keep the aircraft stationary and level on the ground, except for the deliberate-motion phase of the cross-session test.
- Keep `accel_offset_x = 0.0`, `accel_offset_y = 0.0`, and `accel_offset_z = 0.0`.
- Set `debug_log_accel_pipeline = true`, `debug_log_sensor_values = true`, and
  `debug_verbose_logging = true`. The `debug_verbose_logging = true` setting is required:
  the only unconditional per-config bypass proof line (`[DEBUG] Accel calibration BYPASSED`)
  is emitted only inside the `if (debug_verbose_logging)` block at `src/ConfigManager.cpp:239`
  (line emitted at `src/ConfigManager.cpp:245`). Without it, Run B has no runtime proof the
  bypass toggle was honored.
- Do not change `mavlink_*_rate_hz`, PX4 params, sensor noise, calibration code, or scheduler behavior.
- Start from a **fresh PX4 process and a fresh X-Plane process** for Baseline, Run A, and Run B.

Capture for each variant:

- X-Plane `Log.txt` after the run.
- A committed **per-variant config snapshot file** (`baseline-config.ini`,
  `run-a-config.ini`, `run-b-config.ini`) plus its SHA-256 hash. See
  [Config snapshot and hashing](#config-snapshot-and-hashing) for exactly which file is
  hashed and how the three are cross-checked.
- PX4 diagnostics at t0, t5, and t30 after PX4 connects.
- Elapsed seconds from PX4 connect to the first `[ACCEL_CAL] COMPLETE` (or `COMPLETE: none`
  plus the first-30 s `[ACCEL_S5_FINAL]` excerpt as proof it did not complete). **In Run B
  no `[ACCEL_CAL]` line is emitted at all** — see the Run B procedure and
  [Per-run required anchors](#per-run-required-anchors).
- X-Plane FPS for the variant, derived from `[RATE] ... estimated_fps=` log lines (see
  [Per-run required anchors](#per-run-required-anchors)).
- The runtime toggle-proof lines for the variant (see
  [Runtime toggle proof](#runtime-toggle-proof)).
- The session-boundary proof lines (see [Fresh process and session boundary](#fresh-process-and-session-boundary)).
- Any launcher diagnostic report, if available.

Controlled-run requirements that apply to every variant:

- Use a **fresh PX4 process and a fresh X-Plane process** for Baseline, Run A, and Run B
  (only the cross-session variant reconnects PX4 without restarting X-Plane, by design).
- The per-variant config snapshot file and its SHA-256 hash must be committed/attached with
  the evidence.
- Do not change cadence, PX4 params, sensor noise, offsets, or calibration code between runs.

### Config snapshot and hashing

For each variant, copy the installed config to a committed snapshot file before launching:

```bash
cp "~/X-Plane 12/Resources/plugins/px4xplane/64/config.ini" baseline-config.ini   # for Baseline
cp "~/X-Plane 12/Resources/plugins/px4xplane/64/config.ini" run-a-config.ini       # for Run A
cp "~/X-Plane 12/Resources/plugins/px4xplane/64/config.ini" run-b-config.ini       # for Run B
```

- Hash the **committed snapshot file**, not the mutable installed file, so the recorded hash
  matches the attached evidence exactly:

  ```bash
  sha256sum baseline-config.ini run-a-config.ini run-b-config.ini
  ```

- Attach a side-by-side diff of the three snapshots:

  ```bash
  diff baseline-config.ini run-a-config.ini
  diff baseline-config.ini run-b-config.ini
  ```

- **Hard check:** the three hashes must differ, and the diffs must show changes **only** in
  the intended toggle key(s) for that variant (Run A vs Baseline: `accel_auto_calibrate`;
  Run B vs Baseline: `debug_accel_bypass_calibration`). If any other key differs between
  snapshots, the runs are not comparable and the evidence is invalid — re-create the
  snapshots from a single base config changing only the intended toggle.

### Runtime toggle proof

Each variant must carry the exact runtime proof lines below, copied verbatim from `Log.txt`.
Each is emitted by the plugin (verified in `src/`); do not substitute a different line. Note
that `[ACCEL_S2_CAL] ... Calibrated=NO` does **not** prove bypass — `Calibrated` reports
auto-calibration completion state (`src/MAVLinkManager.cpp:182`), not whether the bypass path
was taken.

| Variant | Required runtime proof line | Source |
|---|---|---|
| Baseline | `px4xplane: Accelerometer auto-calibration ENABLED` | `src/ConfigManager.cpp:206` |
| Run A | `px4xplane: Accelerometer manual offset: [0.000, 0.000, 0.000] m/s^2` | `src/ConfigManager.cpp:210` |
| Run B | `px4xplane: Accelerometer auto-calibration ENABLED` **and** `px4xplane: [DEBUG] Accel calibration BYPASSED` | `src/ConfigManager.cpp:206` and `src/ConfigManager.cpp:245` (the BYPASSED line requires `debug_verbose_logging = true`, gate at `src/ConfigManager.cpp:239`) |

Run B keeps `accel_auto_calibrate = true`, so it still emits the auto-calibration ENABLED
line; the `[DEBUG] Accel calibration BYPASSED` line is the distinguishing proof that the
bypass path is active.

### Fresh process and session boundary

- Start a **fresh PX4 process** for each variant (Baseline, Run A, Run B), in addition to a
  fresh X-Plane process. Do not reuse a PX4 process across variants.
- As session-boundary proof, the evidence package must include each run's first
  `[TRANSPORT_EVENT]` lines for `client_connected` (`src/ConnectionManager.cpp:538`) and
  `session_reset` (`src/ConnectionManager.cpp:550`), showing a **new `generation`** value
  (the `generation` field is emitted at `src/ConnectionManager.cpp:225` and is advanced on
  each client accept at `src/ConnectionManager.cpp:526`). A new `generation` confirms the
  variant ran in a fresh transport session and plugin static state did not leak in.

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
debug_verbose_logging = true
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
debug_verbose_logging = true
```

Procedure is identical to Baseline, but save outputs as `run-a-auto-off`. Confirm the
auto-calibration-off state with the runtime proof line `px4xplane: Accelerometer manual
offset: [0.000, 0.000, 0.000] m/s^2` (`src/ConfigManager.cpp:210`).

## Variant 3: Run B, Calibration Bypassed

This short-circuits `AccelCalibration::applyCalibration()` and sends the raw accel path
through. The bypass returns the raw accel at `src/AccelCalibration.cpp:62` (guarded by
`if (ConfigManager::debug_accel_bypass_calibration)` at `src/AccelCalibration.cpp:61`)
**before any `[ACCEL_CAL]` logging** (the `[ACCEL_CAL]` line is emitted later at
`src/AccelCalibration.cpp:238`). As a result, **no `[ACCEL_CAL]` line is emitted at all in
Run B** — there is no `COMPLETE`, no `COMPLETE: none`, and no calibration-status line.

```ini
accel_auto_calibrate = true
accel_offset_x = 0.0
accel_offset_y = 0.0
accel_offset_z = 0.0
debug_accel_bypass_calibration = true
debug_log_accel_pipeline = true
debug_log_sensor_values = true
debug_verbose_logging = true
```

Procedure is identical to Baseline, with these Run B differences:

- **Do NOT wait for `[ACCEL_CAL]`.** It will never appear in Run B. Do not stall the run
  waiting for `COMPLETE`.
- Confirm the bypass is active using the runtime proof line `px4xplane: [DEBUG] Accel
  calibration BYPASSED` (`src/ConfigManager.cpp:245`, requires `debug_verbose_logging = true`).
- Capture the first-30 s `[ACCEL_S5_FINAL]` excerpt (raw, uncalibrated value) as the Run B
  accel evidence in place of any `[ACCEL_CAL]` line.

Save outputs as `run-b-bypass`.

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
debug_verbose_logging = true
```

Procedure:

1. Quit X-Plane completely if it is running.
2. Apply the baseline config.
3. Start X-Plane and PX4.
4. During the first 10 seconds after PX4 connects, deliberately keep the aircraft moving or not level so calibration completes badly or does not complete.
5. Capture PX4 diagnostics at t0, t5, and t30 for session 1.
6. **Record the session-1 scale factor**: the `ScaleFactor=` value from the session-1
   `[ACCEL_S2_CAL]` line (`src/MAVLinkManager.cpp:182`) and, if a `[ACCEL_CAL] COMPLETE` line
   appeared, its reported scale factor (`src/AccelCalibration.cpp:238`). This session-1 value
   is required to prove whether a bad scale factor carries into session 2.
7. Stop PX4, but do not restart X-Plane.
8. Return the aircraft to stationary and level.
9. Start PX4 again and let it reconnect. Record the session-2 `[ACCEL_S2_CAL]` `ScaleFactor=`
   and the `MAVLinkManager session reset complete (generation=..., calibration=...)` line
   (`src/MAVLinkManager.cpp:1000`) to show whether calibration was `reset` or `preserved`
   across the reconnect.
10. Capture PX4 diagnostics at t0, t5, and t30 for session 2.
11. Save outputs as `cross-session-session1` and `cross-session-session2`.

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
| `[ACCEL_CAL]` | `src/AccelCalibration.cpp:238` | YES | Calibration status line; `[ACCEL_CAL] COMPLETE (v2 SCALING) ...` on success. **Not emitted at all in Run B** — bypass returns at `src/AccelCalibration.cpp:61-62` before this emit. |
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
[TRANSPORT_EVENT]
```

`[DIAG_FLIGHTLOOP]` is **NOT in source** — a grep of `src/` and `include/` finds no emitter
for it, so it cannot appear in `Log.txt`. Do not search for it or treat its absence as
meaningful.

### Gravity tolerance band

"Near -9.81" is defined as **Z in the band -9.81 ± 0.2 m/s² (i.e. -10.01 to -9.61 m/s²)**
on the body-frame Z axis while stationary and level. Basis: the plugin uses
`GRAVITY = 9.81` (`src/MAVLinkManager.cpp:31`) as expected gravity; at rest the only
applied noise is the interpolator measurement noise `ACCEL_MEAS_NOISE = 0.05 m/s²`
(`include/SensorInterpolator.h:135`), because vibration noise (σ = 0.1 m/s²,
`src/MAVLinkManager.cpp:42`) is only injected when groundspeed > 0.5 m/s. A ±0.2 m/s²
band is therefore ~4σ over the at-rest noise and tolerant of small attitude error.
Use this numeric band everywhere; do not record subjective "near -9.81" judgements.

Note on filtering: `[ACCEL_S5_FINAL]` is logged after the value passes through
`DataRefManager::applyFilteringIfNeeded(...)` with `ConfigManager::filter_accel_enabled`
(`src/MAVLinkManager.cpp:280-302`). On this branch `filter_accel_enabled` defaults to `false`
and is **not** read from any config key (it is a hardcoded static at
`src/ConfigManager.cpp:65`, never assigned from the ini), so filtering is a no-op for every
variant of this protocol and the ±0.2 m/s² band holds as-is. Do not add a
`filter_accel_enabled` line to the variant config blocks — it would be an inert key that the
plugin ignores. If a future build wires this flag to config and enables it, the band must be
re-qualified because filtering smooths `[ACCEL_S5_FINAL]`.

### Per-run required anchors

All anchors below must be **log-derivable** — taken from `Log.txt`, not from UI counters or
wall-clock estimates — so the reviewer can re-derive them from the attached logs.

For every variant (and every session of the cross-session variant) record, alongside the captures:

- **Connect time (t0 anchor):** the timestamp of the
  `px4xplane: PX4 session reset complete after client accept.` line
  (`src/ConnectionManager.cpp:549`). Use this log line as the canonical PX4-connect instant;
  derive all "elapsed since connect" values from it.
- **Calibration completion time:** elapsed seconds from the connect-time line above to the
  first `[ACCEL_CAL] COMPLETE` line.
  - For **Baseline, Run A, and cross-session**: if no `COMPLETE` line appears, record
    `COMPLETE: none` and attach the `[ACCEL_S5_FINAL]` excerpt covering at least the first
    30 s after connect as proof calibration did not complete.
  - For **Run B**: record the anchor as **`[ACCEL_CAL] ABSENT`**. The bypass returns before
    any `[ACCEL_CAL]` logging (`src/AccelCalibration.cpp:61-62`, before the emit at
    `src/AccelCalibration.cpp:238`), so the tag is emitted zero times. `ABSENT` is distinct
    from Baseline/Run A's `COMPLETE: none` (where `[ACCEL_CAL]` lines exist but none say
    `COMPLETE`). Attach the first-30 s `[ACCEL_S5_FINAL]` excerpt as the Run B accel evidence.
- **Reported scale factor** from the `[ACCEL_CAL] COMPLETE` line (or `n/a` if none; `n/a
  (ABSENT)` for Run B).
- **`[ACCEL_S5_FINAL]` Z band check:** whether Z is within -9.81 ± 0.2 m/s² during the
  first seconds after connect (record the actual min/max Z observed, not just yes/no).
- **X-Plane FPS** for the variant, derived from the `[RATE] ... estimated_fps=N` log lines
  (`src/px4xplane.cpp:643`; one emitted every 1000 HIL_SENSOR messages,
  `src/px4xplane.cpp:634`). Record the **mean and min** of the `estimated_fps` values across
  the stationary portion of the run — do **not** use the on-screen UI FPS counter. Record one
  mean/min pair per variant: Baseline, Run A, Run B (and per session for cross-session). Runs
  whose mean FPS differs by more than ~20% from Baseline are not directly comparable and must
  be flagged.

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

#### Hard precondition: EKF2 must be running before any poisoning verdict

**Before reading the verdict table, check the EKF2 accel/IMU update count from `ekf2 status`
at t5 and t30 in every variant.** If that update count is **0 across ALL variants** (Baseline,
Run A, and Run B), EKF2 never consumed the accelerometer stream, so nothing in this run can
poison the estimator and no accel-value comparison is estimator-meaningful.

In that case the verdict is forced to **INCONCLUSIVE / NO_EKF2**, regardless of any
`[ACCEL_S5_FINAL]` Z-band or validator-line differences between variants. **No row in the
table below may conclude poisoning (CONFIRMED, SUSPECTED, or VALUE-POISONING SIGNAL) while the
EKF2 accel/IMU update count is 0 everywhere.** First fix why EKF2 is not updating (e.g. the
HITL cadence / lockstep issue in [HITL_DIAGNOSTICS.md](HITL_DIAGNOSTICS.md)) and re-run the
protocol; only then is the table valid.

The EKF2 update count is non-zero in at least one variant is therefore a **gate** on the
entire scoring table. Record the per-variant EKF2 accel/IMU update count alongside every
verdict.

Use this table for the PR/issue verdict (only after the EKF2 gate above passes):

| Observation | Interpretation | Verdict |
|---|---|---|
| **EKF2 accel/IMU update count is 0 in ALL variants at t5 and t30** | EKF2 never ingested accel; no estimator-level poisoning is possible and no comparison is valid | **INCONCLUSIVE / NO_EKF2** (overrides every other row — never conclude poisoning) |
| Baseline shows `Accel #0 fail` or validator failsafe YES at t5, while **Run B** is clean at t5/t30 | Calibration transform poisons validator during startup (primary signal) | POISONING CONFIRMED |
| Baseline fails but only **Run A** (not Run B) is clean | Weaker, collection-window-specific signal; corroborating only — do not declare poisoning on Run A alone | POISONING SUSPECTED (needs Run B) |
| Baseline and **Run B** show the same validator failure | Failure is upstream of the calibration transform, such as raw values, sign, timing, or cadence | POISONING DISPROVED for this symptom |
| Cross-session session 2 inherits a prior bad scale factor and validator remains failed | Preserved calibration state is harmful across reconnects | CROSS-SESSION POISONING CONFIRMED |
| Cross-session session 2 recalibrates or validator clears while level | Preserved state was not harmful in this scenario | CROSS-SESSION POISONING DISPROVED |
| Baseline `[ACCEL_S5_FINAL]` Z is outside -9.81 ± 0.2 m/s², but **Run B** Z is within -9.81 ± 0.2 m/s² | Calibration transform has a value-poisoning signal | VALUE-POISONING SIGNAL |

Every verdict must cite the per-variant EKF2 accel/IMU update count, the per-variant FPS
(mean/min from `[RATE]`), and the config-snapshot SHA-256 hashes used, so the reviewer can
confirm EKF2 was actually running, the runs are comparable, and the intended toggle was active.

## Hand-Off Package

Attach or paste:

- The per-variant config snapshot files (`baseline-config.ini`, `run-a-config.ini`,
  `run-b-config.ini`) and their SHA-256 hashes, plus the snapshot diffs.
- X-Plane log excerpts filtered to the current session boundary, including the
  `[TRANSPORT_EVENT]` `client_connected` / `session_reset` lines (with `generation`) and the
  per-variant runtime toggle-proof lines.
- t0/t5/t30 PX4 captures for every variant, including the `ekf2 status` accel/IMU update count.
- The per-variant EKF2 gate result (PASS if update count > 0 in at least one variant; else
  NO_EKF2).
- A short verdict using the scoring table.

Keep issue #16 open after attaching the evidence so reviewers can compare the four variants.
