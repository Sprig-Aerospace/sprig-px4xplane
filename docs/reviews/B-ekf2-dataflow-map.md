# B — PX4 Sensor → EKF2 Dataflow Map & Zero-Update Gate Identification

**Issue:** #7 · **Runner:** Claude · **Cross-review:** Kimi · **Status:** drafted
**Scope:** dataflow MAPPING and gate identification. No fixes, no param tuning. A model for the
[DIAG] run (Issue E) to test.
**Built on Issue A's evidence contract** — every "is it zero?" claim below must be read from
**post-session-boundary** evidence (lines after the latest `client_connected`+`session_reset`,
which carry `generation`). Today's bundle scalars are distrusted per A §9.

---

## 0. TL;DR — most-likely first-failing gate

**This is PX4 SITL, not HITL.** No airframe sets `SYS_HITL` (grep over `config/px4_params/*` =
empty). The X-Plane plugin is the external simulator feeding PX4 SITL's `simulator_mavlink` over
TCP 4560 via `HIL_SENSOR`/`HIL_GPS`/`HIL_STATE_QUATERNION`.

**Yet every airframe enables the SIH-style simulated-sensor modules** —
`SENS_EN_GPSSIM=1`, `SENS_EN_BAROSIM=1`, `SENS_EN_MAGSIM=1` (tb2), `SENS_EN_ARSPDSIM=1`
(`5020_xplane_alia250:115-116`, `5002_xplane_tb2:104-107`, `5001_xplane_cessna172:82-84`, etc.).
Those modules synthesize `sensor_gps`/`sensor_baro`/`sensor_mag` from **ground-truth** topics
(`vehicle_local_position`, etc.) that a lockstep external simulator does **not** publish.

**→ Hypotheses for EKF2 = 0 updates and missing `vehicle_global_position`
(re-weighted after Kimi cross-review, see §8):**
- **PRIMARY — IMU-integration / validator / lockstep gates (gates 2–4).** Most consistent with the
  symptom "`sensor_accel`/`sensor_gyro`/`vehicle_imu`/`vehicle_gps_position` exist but EKF2 = 0
  updates": `vehicle_imu` exists but its callback never drives EKF2 (multi-instance init /
  selector failure, gate 2); or an IMU validator latch (`Accel #0 fail: STALE/TIMEOUT`, gate 3, →
  Issue D); or a lockstep/timestamp stall (gate 4, → Issue A's hardcoded-drift blind spot).
- **SECONDARY — sensor-source contention (`SENS_EN_*SIM`).** The `sensor_*_sim` modules publish the
  same topics and `SensorBaroSim` collides on the **identical `device_id` 6620172** that
  `simulator_mavlink` uses (Kimi §8.1 #2-3). BUT they are **dormant at cold startup** — they
  publish only once `vehicle_global_position`/`vehicle_local_position` (EKF2 *outputs*) exist, which
  they don't yet. So they cannot be the *first* cause of zero updates; they are a genuine
  double-publisher/contention hazard **once EKF2 starts and the ground-truth loop closes**. Still
  must be tested (empty `vehicle_air_data`/`vehicle_magnetometer` at +5/+30 s while raw sensor
  topics are healthy would implicate the sensors-module voter, gate 3/4 — not the SIM modules).

All are hypotheses to TEST via §5, not findings, and **not** a fix authorization.

---

## 1. Plugin-side dataflow (verified from source)

```
X-Plane datarefs ──► MAVLinkManager ──► MAVLink msg ──► TCP 4560 ──► PX4 simulator_mavlink
─────────────────    ──────────────     ───────────     ─────────    ──────────────────────
g_axil/g_side/g_nrml ► computeAcceleration()/setAccelerationData()  ─┐
  (MAVLinkManager.cpp:151-153, :77-82)                               │
Prad/Qrad/Rrad ──────► setGyroData()  (:1086-1095)                   ├► HIL_SENSOR (msgid 107)
ISA baro from MSL ───► setPressureData() (:~1096+)                   │   sendHILSensor() :375-497
WMM2025 mag ─────────► setMagneticFieldData()                        │   @ 200 Hz target (Alia250)
OAT temp ────────────► hil_sensor.temperature (:402)                ─┘
lat/lon/alt/vel ─────► sendHILGPS() → HIL_GPS (msgid 113) :554        @ 10 Hz
attitude quat ───────► sendHILStateQuaternion() → HIL_STATE_QUATERNION (msgid 115) :664 @ 50 Hz
```

**HIL_SENSOR `fields_updated` bitmask (`MAVLinkManager.cpp:404-419`)** — bits 0–12 all set:

| Bit | Field | Set? | Note |
|---|---|---|---|
| 0–2 | XACC/YACC/ZACC | ✔ | from `computeAcceleration()` (gload, calibrated — see Issue D) |
| 3–5 | XGYRO/YGYRO/ZGYRO | ✔ | Prad/Qrad/Rrad + noise |
| 6–8 | XMAG/YMAG/ZMAG | ✔ | WMM2025 |
| 9 | ABS_PRESSURE | ✔ | ISA |
| 10 | DIF_PRESSURE | ✔ | pitot/airspeed |
| 11 | PRESSURE_ALT | ✔ | — |
| 12 | TEMPERATURE | ✔ | — |

**Q6 verdict:** the bitmask asserts a *full* IMU+baro+mag+diff-pressure sensor set every frame.
That is internally consistent (identification only — no change proposed). The risk is **not** a
missing bit; it is whether PX4 routes these fields to EKF2's expected topics given the SIM-module
config (§0). One nuance to flag for [DIAG]: bit 11 `PRESSURE_ALT` and bit 10 `DIF_PRESSURE` are
asserted as fresh every frame — if PX4's validator expects diff-pressure only when an airspeed
sensor is modeled, an over-asserted bitmask is worth noting, but it is **not** a likely zero-update
cause on its own.

---

## 2. Q1/Q2 — Which topics SHOULD HIL_SENSOR create, and what does EKF2 consume?

**Q1 — topics `simulator_mavlink` (SITL) creates from HIL_SENSOR:** in the PX4 SITL
`simulator_mavlink` path, an inbound `HIL_SENSOR` publishes the primary sensor topics:
`sensor_accel`, `sensor_gyro`, `sensor_baro`, `sensor_mag` (and differential pressure →
`differential_pressure`). `HIL_GPS` → `sensor_gps` (→ `vehicle_gps_position`).

**Q2 — EKF2's actual input topics (this build):** EKF2 does **not** consume `sensor_accel`/
`sensor_gyro` directly, and modern PX4 does **not** feed EKF2 from `sensor_combined` for IMU.
EKF2 subscribes to:
- **`vehicle_imu`** (+ `vehicle_imu_status`) — produced by the `sensors`/IMU-integrator from
  `sensor_accel`+`sensor_gyro`, selected via `vehicle_imu` instance / `estimator_selector_status`.
- **`vehicle_air_data`** — baro aiding (from `sensor_baro` via the baro aggregator).
- **`vehicle_magnetometer`** — mag aiding (from `sensor_mag`).
- **`vehicle_gps_position`** — GPS aiding (from `sensor_gps`).
- airspeed (`airspeed_validated`) for fixed-wing/VTOL aiding only.

> **This Q2 answer is the single assumption most worth Kimi challenging:** confirm against the
> actual deployed PX4 version whether EKF2 reads `vehicle_imu` vs `sensor_combined`, and whether
> the baro/mag/gps aggregators are the `*_sim` modules or the standard `sensors` drivers when
> `SENS_EN_*SIM=1`. The gate table (§4) is structured so the [DIAG] commands resolve this
> empirically rather than relying on this map's PX4-internal claims.

---

## 3. Q3/Q4 — Can sensors publish while EKF2 gets no valid IMU? Can a validator latch?

**Q3 — Yes.** `sensor_accel`/`sensor_gyro` can publish while EKF2 receives no *valid* IMU when:
- the IMU integrator (`vehicle_imu`) never selects/validates the instance (data-validity gate);
- timestamps are non-monotonic or stale relative to PX4's lockstep clock — **note Issue A:**
  the plugin's `time_usec` is a step-clock accumulation (`TimestampProvider::getTimestampUsec`,
  `MAVLinkManager.cpp:392`) and `drift_ms` is hardcoded 0, so a timing-stale condition would be
  **invisible in today's bundle**;
- the sensor validator marks accel/gyro `STALE/TIMEOUT` (Issue D's poisoning window), which gates
  the IMU before EKF2 ever sees it.

**Q4 — Yes, a validator failsafe can LATCH.** PX4's sensor validator / `commander` preflight can
trip on an early STALE/TIMEOUT or high-bias condition during the startup window and **not
self-clear** even after samples normalize, blocking EKF2 start. Crucially (Issue A §6) the
bundle's failsafe "YES" is an **undated, un-partitioned** PX4 paste — a latched-then-stale trip is
indistinguishable from a current one. So "validator failsafe = YES" **cannot today** be attributed
to a current condition; it must be re-read at fixed offsets (A §7) to separate latched-historical
from persistent. This is the exact seam Issue D's A/B is designed to probe.

---

## 4. Gate table — ordered preconditions: sensor publish → `vehicle_global_position`

Each row: the gate → which topic/param/check satisfies it → the **PX4 command + log signature**
that confirms pass/fail. (Ordered roughly by EKF2 startup dependency.)

| # | Gate (must pass for EKF2 to advance) | Satisfied by | Confirm PASS / FAIL signature |
|---|---|---|---|
| 1 | **IMU present & publishing** | `sensor_accel`,`sensor_gyro` from HIL_SENSOR | `listener sensor_accel`/`sensor_gyro` → nonzero, advancing `timestamp`. FAIL = no topic / frozen ts. |
| 2 | **IMU integrated & selected** | `vehicle_imu`(+`_status`), `IMU_INTEG_RATE` (200 Alia / 100 others, `:181`) | `listener vehicle_imu` advancing; `listener vehicle_imu_status`; `ekf2 status` shows IMU selected. FAIL = `vehicle_imu` static / not selected. |
| 3 | **IMU data valid (no validator trip)** | sensor validator; accel/gyro plausibility (Issue D) | `commander check`; sensor-validator status; absence of `Accel #0 fail: STALE/TIMEOUT`. FAIL = validator failsafe YES (re-confirm current via A §7 offsets). |
| 4 | **Timestamps monotonic vs lockstep** | `TimestampProvider` step-clock (`:392`) | EKF2 `time_slip` ~0; no lockstep stall. FAIL = growing `time_slip` / EKF2 stalls. (A: drift hardcoded 0 → not visible plugin-side.) |
| 5 | **Baro aiding available** | `vehicle_air_data` (from `sensor_baro`) **OR** `sensor_baro_sim`? | `listener vehicle_air_data` advancing; `EKF2_BARO_CTRL=1` (`:125`). FAIL = empty `vehicle_air_data` → §0 SIM-module mismatch. |
| 6 | **Mag aiding available** | `vehicle_magnetometer` (from `sensor_mag`); `EKF2_MAG_TYPE=0` (`:162`) | `listener vehicle_magnetometer`; yaw init in `estimator_status_flags`. FAIL = no mag → yaw never aligns. |
| 7 | **GPS present & valid** | `vehicle_gps_position` (from HIL_GPS), `fix_type`,`satellites`,`eph/epv` (`:554`) | `listener vehicle_gps_position` → `fix_type>=3`, sats>0. FAIL = no fix / starved by `SENS_EN_GPSSIM`. |
| 8 | **GPS origin / EKF2 aiding init** | EKF2 sets local origin from GPS; `EKF2_AGP_CTRL`,`EKF2_GPS_*` | `estimator_status_flags` GPS aiding bits set; origin set msg. FAIL = origin never set → no global position. |
| 9 | **EKF2 update events > 0** | gates 2–8 satisfied | `ekf2 status` update count > 0; `estimator_status`/`estimator_sensor_bias` non-empty. FAIL = 0 updates, empty flags/bias (current symptom). |
| 10 | **`vehicle_global_position` emitted** | EKF2 converged + origin | `listener vehicle_global_position` advancing. FAIL = topic never published (current symptom). |

**Reading the current symptom against the table:** "topics exist (`sensor_accel`,`sensor_gyro`,
`vehicle_imu`,`vehicle_gps_position`) but EKF2 = 0 updates and `estimator_status_flags`/
`estimator_sensor_bias` empty" places the first failure at **gate 3, 5/6, or 8** — i.e. either an
**IMU validator latch** (gate 3, → Issue D) or an **aiding-source init failure** (gates 5–8, →
§0 SIM-module mismatch). Gates 1–2 appear to PASS (topics exist), which is exactly why
"publishing ≠ consumption": the IMU integrates but aiding/validation gates downstream hold EKF2
at zero.

---

## 5. Q7 + Q5 — Discriminating commands for the [DIAG] run (feeds Issue E)

Run in the PX4 shell at the A-contract fixed offsets (connect / +5 s / +30 s) so transient vs
persistent separates:

```text
# Source & validity (gates 1-4)
listener sensor_accel 5
listener sensor_gyro 5
listener vehicle_imu 10
listener vehicle_imu_status 3
param show IMU_INTEG_RATE
commander check                      # validator/preflight summary (current state)
dmesg | grep -i "accel\|gyro\|STALE\|TIMEOUT\|validator"   # latch signature

# IMU integration / selector / early EKF2 output (gates 2-4) — the PRIMARY hypothesis probes
listener vehicle_attitude 5          # EKF2 emits this on the FIRST valid IMU callback; absent = IMU path blocked pre-EKF2
listener estimator_selector_status 3 # multi-instance: confirm an EKF instance was selected
param show EKF2_MULTI_IMU            # if 0 with SENS_IMU_MODE=1, EKF2 falls back to sensor_combined (reinterpret gate table)
param show SENS_IMU_MODE
work_queue status                    # are sensor_baro_sim/gps_sim/mag_sim actually running?
commander status
commander check

# Aiding sources (gates 5-8) — the SIM-module CONTENTION test (§0 secondary)
listener vehicle_air_data 5
listener vehicle_magnetometer 5
listener vehicle_gps_position 5
listener sensor_baro 5               # read device_id: simulator_mavlink vs *_sim (baro shows id 6620172 if both publish)
listener sensor_mag 5               # read device_id / devtype to spot *_sim contention
listener sensor_gps 5
listener differential_pressure 3     # fixed-wing/VTOL (Cessna/TB2/Alia/qtailsitter)
listener airspeed_validated 3
param show SENS_EN_GPSSIM
param show SENS_EN_BAROSIM
param show SENS_EN_MAGSIM
uorb top -1 sensor_baro              # CORRECTED: `uorb status <topic>` errors; use `uorb top -1 <topic>`
uorb top -1 vehicle_gps_position
uorb status                          # full dump (no per-topic arg)

# EKF2 itself (gates 9-10)
ekf2 status
listener estimator_status 5
listener estimator_status_flags 5
listener estimator_sensor_bias 3
listener vehicle_global_position 5
uorb top                            # publish rates of all the above
```

**Q7 — log signatures that distinguish failure classes:**
| Failure class | Distinguishing signature |
|---|---|
| timing-stale | EKF2 `time_slip` grows; `vehicle_imu` ts non-monotonic; lockstep stall |
| data-bias | `High Accelerometer Bias` / `estimator_sensor_bias` saturates; accel ≠ ~[0,0,-9.81] at rest |
| missing IMU aggregation | `sensor_accel` advances but `vehicle_imu` static / not selected |
| GPS-origin failure | `vehicle_gps_position` valid but `estimator_status_flags` GPS-aiding bit never set; no origin |
| yaw/mag failure | `vehicle_magnetometer` empty or yaw-align bit never set |
| commander gating | `commander check` reports failsafe; arming denied; validator latch in dmesg |
| **SIM-module mismatch (§0)** | `uorb status sensor_baro`/`vehicle_gps_position` shows a `*_sim` publisher (or zero publishers) instead of `simulator_mavlink`; aiding topics empty despite HIL_GPS/baro being sent |
| stale-log contamination | per Issue A — evidence predates latest `session_reset`; cross-check `generation` |

**Q5 (`fields_updated`) restated:** bits set match a full IMU+baro+mag+diffpress set
(`:404-419`); identification only, no change. The set is *not* the likely zero-update cause — the
aiding-route/validator gates are.

---

## 6. Single most-likely first-failing gate + confirming evidence

**Most likely (corrected after Kimi cross-review): gate 2–4 — IMU integration / validator latch /
lockstep-timestamp stall**, NOT the SIM-module aiding-starvation originally proposed. Rationale:
the `sensor_*_sim` modules are dormant at cold start (they need EKF2 outputs that don't exist yet,
Kimi §8.1 #3,#6), so they cannot be the *first* failure. The symptom — `vehicle_imu` and
`vehicle_gps_position` exist but EKF2 = 0 updates with empty `estimator_status_flags`/
`estimator_sensor_bias` — fits **(a)** `vehicle_imu` existing but its callback not driving EKF2
(multi-instance selector failure, gate 2), **(b)** an IMU validator latch
(`Accel #0 fail: STALE/TIMEOUT`, gate 3 → Issue D), or **(c)** a lockstep/timestamp stall
(gate 4 → Issue A's hardcoded-drift blind spot).

**Confirming evidence (the kill shots), at +5 s and +30 s:**
- `listener vehicle_attitude` — if **absent**, EKF2 never completed even its first IMU callback →
  the block is at gate 2/3/4 (pre-aiding). This is the single most decisive early probe.
- `listener estimator_selector_status` + `ekf2 status` — no selected instance → gate 2.
- IMU validator / `commander check` + dmesg `STALE/TIMEOUT` → gate 3 (→ Issue D's A/B separates
  latched-historical from current).
- EKF2 `time_slip` growing / non-monotonic `vehicle_imu` ts → gate 4.

**Secondary: SIM-module contention (gates 5–8).** If `vehicle_attitude` IS present but
`vehicle_air_data`/`vehicle_magnetometer`/`vehicle_gps_position` are **empty while `sensor_baro`/
`sensor_mag`/`sensor_gps` from `simulator_mavlink` are healthy**, the first failure is in the
sensors-module voter/aggregator (gate 3/4 region), not the SIM modules. If after EKF2 starts the
raw sensor topics show a `*_sim` publisher or the colliding baro `device_id` 6620172, the
SIM-module contention hypothesis is confirmed as a **secondary** fault. B, D, and A converge on
gate 3/4; A's fixed-offset capture is what separates "latched-historical" from "currently failing".

---

### Runner response to cross-review (Claude)
Kimi's notes are **accepted and folded in**: (1) the primary/secondary hypothesis weighting above is
corrected — IMU-integration/validator/lockstep (gates 2–4) lead, SIM-module contention is secondary
and cold-start-dormant; (2) the invalid `uorb status <topic>` commands are replaced with
`uorb top -1 <topic>` in §5, and the missing probes (`vehicle_attitude`,
`estimator_selector_status`, `work_queue status`, per-topic `device_id` listeners, `commander
status/check`, `differential_pressure`/`airspeed_validated`, `EKF2_MULTI_IMU`/`SENS_IMU_MODE`) are
added; (3) the EKF2-input claim (`vehicle_imu`) stands, confirmed against PX4 v1.14, with the
`sensor_combined` fallback flagged when `EKF2_MULTI_IMU=0`. No source/param changed — corrections
are to the diagnostic plan only.

---

## 7. Forbidden-change boundary (self-audit)
Mapping and gate identification only. No EKF2/SENS/IMU param tuned (expected values cited for
identification, e.g. `IMU_INTEG_RATE` match, `EKF2_BARO_CTRL=1` — none changed). No sensor
value/noise/calibration touched. No catch-up/multi-send/async proposed. The `SENS_EN_*SIM`
observation is flagged as a **hypothesis for [DIAG] to test**, explicitly not a recommendation to
flip the param. ✔

---

## 8. Cross-review sign-off (Kimi)

Cross-review performed against: `src/MAVLinkManager.cpp:375-497`, `:404-419`, `:554-611`, `:664-699`,
`:77-82`, `:1086-1096`; `config/px4_params/*` (SYS_HITL, SENS_EN_*, EKF2_*, IMU_INTEG_RATE);
`config/config.ini`; PX4-Autopilot v1.14.0 reference sources (`SimulatorMavlink.cpp/.hpp`,
`EKF2.cpp`, `VehicleIMU.cpp`, `VehicleAirData.cpp`, `VehicleMagnetometer.cpp`,
`VehicleGPSPosition.cpp`, `SensorBaroSim.cpp`, `SensorGpsSim.cpp`, `SensorMagSim.cpp`,
`src/drivers/drv_sensor.h`, `src/systemcmds/uorb/uorb.cpp`).

### 8.1 Assumptions challenged

| # | Artifact claim | Verdict | Evidence / reasoning |
|---|----------------|---------|----------------------|
| 1 | **EKF2 consumes `vehicle_imu` (not `sensor_combined`) in this build.** | **CONFIRMED** for the default PX4 v1.14 SITL multi-instance path. | `EKF2.cpp` v1.14 `Run()` registers `_vehicle_imu_sub.registerCallback()` when `CONFIG_EKF2_MULTI_INSTANCE` is set and reads `vehicle_imu_s` (`delta_angle`, `delta_velocity`). The non-multi fallback consumes `sensor_combined`. Empirical disambiguation: `listener vehicle_imu 10` advancing + `ekf2 status` showing IMU instances = multi-instance `vehicle_imu` path; static `vehicle_imu` with advancing `sensor_combined` would indicate fallback. |
| 2 | **`HIL_SENSOR` → `sensor_accel`/`sensor_gyro`/`sensor_baro`/`sensor_mag`; `HIL_GPS` → `sensor_gps`.** | **CONFIRMED**. | `SimulatorMavlink::update_sensors()` v1.14.0 publishes via `PX4Accelerometer`/`PX4Gyroscope`/`PX4Magnetometer` and direct `sensor_baro`/`differential_pressure` pubs; `handle_message_hil_gps()` publishes `sensor_gps`. Device IDs used by `simulator_mavlink`: accel/gyro `DRV_IMU_DEVTYPE_SIM` (0x14), baro `DRV_BARO_DEVTYPE_BAROSIM` (0x65, id 6620172), mag `DRV_MAG_DEVTYPE_MAGSIM` (0x03). |
| 3 | **SENS_EN_GPSSIM/BAROSIM/MAGSIM conflict with `simulator_mavlink` and starve/contend aiding topics.** | **PLAUSIBLE BUT OVERSTATED** as the primary startup failure. | The `sensor_*_sim` modules (v1.14.0 `src/modules/simulation/sensor_*_sim/`) do publish the same uORB topics and, critically, `SensorBaroSim` uses the **identical** `device_id = 6620172` (`DRV_BARO_DEVTYPE_BAROSIM`, bus 1, addr 4) that `simulator_mavlink` uses. `SensorMagSim` uses the same `devtype` (`DRV_MAG_DEVTYPE_MAGSIM`) as `simulator_mavlink`. However, at **cold startup** these sim modules are **dormant**: `SensorBaroSim` and `SensorGpsSim` only publish when `vehicle_global_position`/`vehicle_local_position` update, and `SensorMagSim` only after it has received a valid global position. Those are EKF2 *outputs*, which do not yet exist. Therefore they cannot be the *initial* cause of zero EKF2 updates. They become a genuine **double-publisher / instance-contention** hazard once EKF2 starts and the ground-truth loop closes. |
| 4 | **SENS_EN_*SIM might be the INTENDED path for X-Plane.** | **REFUTED** for this architecture. | The PX4 SIH/SIM-sensor path is documented for `PX4_SIMULATOR=sihsim` (internal physics). This repo builds `px4_sitl_default` and connects via TCP 4560 through `simulator_mavlink` (`setup_px4_sitl.sh:88-89`, `README.md:212`). The SIH-style params are present in the airframe configs but the corresponding modules are not the intended source for an external MAVLink simulator. |
| 5 | **Gate table ordering is correct.** | **ACCEPT-WITH-NOTES**. | The sequence IMU → `vehicle_imu` → validation → aiding → EKF2 origin → global position is sound. Missing gates that matter for this symptom: (i) `differential_pressure`/`airspeed_validated` for fixed-wing/VTOL airframes; (ii) `commander`/`health_and_arming_checks` preflight gate; (iii) `estimator_selector_status` in multi-instance builds; (iv) early `vehicle_attitude` output (EKF2 publishes it on the first successful IMU callback, before global position). |
| 6 | **Single most-likely first-failing gate = gates 5–8 (aiding-source init / SENS_EN_*SIM mismatch).** | **PARTIALLY REFUTED** — a less-likely primary than gates 2–4. | Because `sensor_*_sim` modules are dormant until EKF2 outputs exist, the symptom "`sensor_accel`/`sensor_gyro`/`vehicle_imu`/`vehicle_gps_position` exist but EKF2 = 0 updates" is more consistent with: (a) `vehicle_imu` existing but its callback not driving EKF2 (multi-instance init / selector failure, gate 2); (b) IMU validator latch (`Accel #0 fail: STALE/TIMEOUT`, gate 3); or (c) lockstep/timestamp stall (gate 4). Empty `vehicle_air_data`/`vehicle_magnetometer` at startup is still a valid *result* to test (gates 5–6), but the *cause* is more likely validator/timing than SIM-module starvation. The SIM-module contention is a strong **secondary** hypothesis once EKF2 begins producing position/attitude. |
| 7 | **`uorb status sensor_baro` / `uorb status vehicle_gps_position` are valid DIAG commands.** | **REFUTED**. | PX4 v1.14 `uorb_main()` accepts `uorb status` (no args) or `uorb top [filter...]` (`src/systemcmds/uorb/uorb.cpp`). Topic-specific status requires `uorb top -1 sensor_baro` (single-shot rate view) or `uorb status` for all topics. `uorb status <topic>` will error. |
| 8 | **`fields_updated` bits 0–12 set matches PX4 expectation.** | **CONFIRMED** (identification only). | `MAVLinkManager.cpp:404-419` sets all HIL_SENSOR bits; `SimulatorMavlink::update_sensors()` masks against `SensorSource::ACCEL|GYRO|MAG|BARO|DIFF_PRESS`. The full set is accepted. The risk is not a missing bit but whether over-asserting `DIFF_PRESS`/`PRESSURE_ALT` every frame confuses downstream validation; this is noted in the artifact and is not a zero-update cause on its own. |

### 8.2 Forbidden-change boundary intact?

**Y.** This review read source and parameters only; it proposes no code changes, no param tuning, no sensor-value/noise/calibration changes, and no catch-up/multi-send/async additions.

### 8.3 Gaps the runner missed

1. **DIAG commands that will not run as written:** replace `uorb status sensor_baro` / `uorb status vehicle_gps_position` with `uorb top -1 sensor_baro`, `uorb top -1 vehicle_gps_position`, and a single `uorb status` dump.
2. **Missing DIAG commands to disambiguate the SIM-module hypothesis:**
   - `work_queue status` — shows whether `sensor_baro_sim`, `sensor_gps_sim`, `sensor_mag_sim` are actually running.
   - `listener sensor_baro` / `listener sensor_mag` / `listener sensor_gps` — read the `device_id` field to confirm whether the publisher is `simulator_mavlink` or a `*_sim` module (baro will show identical id 6620172 if both publish).
   - `listener vehicle_attitude` — EKF2 publishes this immediately on the first valid IMU callback; absence means the IMU path is blocked before EKF2 ever runs.
   - `listener estimator_selector_status` — required in multi-instance SITL to confirm an EKF instance has been selected.
   - `listener differential_pressure` / `listener airspeed_validated` — needed for fixed-wing/VTOL airframes (Cessna, TB2, Alia, qtailsitter).
   - `commander status` / `commander check` — explicit commander gating gate.
3. **Mechanism refinement for §0:** the `sensor_*_sim` modules are a startup-*dormant* double-publisher hazard, not a startup-*starvation* hazard. The DIAG plan should capture `vehicle_air_data`, `vehicle_magnetometer`, and `vehicle_gps_position` at +5 s and +30 s; if they are empty while `sensor_baro`/`sensor_mag`/`sensor_gps` from `simulator_mavlink` are healthy, the first failure is in the sensors-module voter/aggregator (gate 3/4), not in the SIM modules. If `sensor_baro`/`sensor_mag` themselves are absent or dominated by `*_sim` publishers after EKF2 starts, then the SIM-module contention hypothesis is confirmed.
4. **EKF2 input disambiguation:** add `param show EKF2_MULTI_IMU` / `param show SENS_IMU_MODE` to the DIAG list; if `EKF2_MULTI_IMU=0` and `SENS_IMU_MODE=1`, EKF2 falls back to `sensor_combined` and the vehicle_imu-centric gate table needs to be reinterpreted.

### 8.4 Cross-review verdict

**ACCEPT-WITH-NOTES.** The dataflow map and the `vehicle_imu` EKF2 input claim are correct for PX4 v1.14 multi-instance SITL. The gate table is a useful diagnostic scaffold but under-weights the IMU-integration/validator/lockstep gates and over-weights the SENS_EN_*SIM aiding-starvation hypothesis as the *first* failure. The SENS_EN_*SIM params are a real source of topic/device-id contention that the [DIAG] run must test, but the current symptom is more likely explained by gates 2–4 unless post-boundary evidence shows empty `vehicle_air_data`/`vehicle_magnetometer` while raw sensor topics are healthy. Fix the invalid `uorb status <topic>` commands and add the missing DIAG probes before Issue E consumes this artifact.
