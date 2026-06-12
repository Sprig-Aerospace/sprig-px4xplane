# C – Flight-loop workload audit

**Scope:** Per-frame cost inventory and pacing hypothesis for `MyFlightLoopCallback`. Read-only audit; no fixes, no catch-up, no sensor/param changes. Cross-review by Claude pending.

---

## Evidence hygiene (from Issue A contract)

The diagnostic bundle’s scalar cadence numbers **must not be trusted** for this analysis:

* `px4xplane.cpp:629-635` computes `actualRate = 1.0 / actualDt_sec` and prints a mean of those reciprocals. A mean-of-reciprocals overstates the true rate whenever `dt` jitters.
* `TimestampProvider.cpp:76` hardcodes `drift_usec = 0`, so any reported `drift_ms` is meaningless.
* `px4xplane.cpp:636-638` reads `sim/operation/misc/frame_rate_period` only once every 1000 sensor messages, so the printed `estimated_fps` is a spot sample, not a min/mean over the interval.
* The bundle parses the whole `Log.txt` with no session boundary, mixing backgrounded/disconnected sessions with active HITL sessions.

**Numbers to trust instead:**

* `TimestampProvider::noteMessageTimestamp(...)` in `TimestampProvider.cpp:108-155` already maintains per-kind (`HIL_SENSOR`, `HIL_GPS`, etc.) `dt` histograms (`hist_*_usec` buckets, min/max/mean, sample count). These are session-scoped because `TimestampProvider::reset()` clears them on reconnect.
* `ConnectionManager.cpp:222-249` emits raw `[TRANSPORT_EVENT]` JSON lines carrying `"generation"`, `"flight_loop_elapsed_sec"`, `"flight_loop_elapsed_since_last_sec"`, and `"estimated_fps"`. Filter by `generation` to isolate one PX4 session.

All cadence reasoning below uses those two sources, not the bundle’s `[RATE]` summary.

---

## Per-frame cost inventory

`MyFlightLoopCallback` is registered at `src/px4xplane.cpp:208` with interval `-1.0f` (every X-Plane frame). The body is `src/px4xplane.cpp:543-689`.

| Operation | Location | Cost class | Contribution to slow frames |
|---|---|---|---|
| `ConnectionManager::noteFlightLoopTiming(...)` | px4xplane.cpp:547-551 | Cheap | Stores three floats and an FPS estimate. Negligible. |
| Wait-for-connection path (`XPLMFindDataRef` + `XPLMGetDataf` for `sim/time/total_running_time_sec`, HUD update, `tryAcceptConnection`) | px4xplane.cpp:553-591 | String-lookup + cheap | Only active while waiting. After connect, the branch falls through. |
| `ConnectionManager::tryAcceptConnection()` (stale-client replacement) | px4xplane.cpp:598 | Cheap | One non-blocking `accept()` per frame when connected. Negligible. |
| `ConnectionStatusHUD::notifyConnected()` | px4xplane.cpp:601 | Cheap | Sets a bool. Negligible. |
| `pluginClockSec += inElapsedSinceLastCall` | px4xplane.cpp:611 | Cheap | Double add. |
| `XPLMFindDataRef("sim/time/total_flight_time_sec")` + `XPLMGetDataf` | px4xplane.cpp:613 | **Per-frame string lookup** | This is an uncached string-keyed lookup executed on every connected frame. Likely a few microseconds, but it is a pure avoidable cost. |
| `TimestampProvider::advanceSimulationClock(...)` | px4xplane.cpp:614 | Cheap | Branch + cap + add. |
| `MAVLinkManager::sendHILSensor(0)` (eligible every `TARGET_SENSOR_PERIOD` ≈ 5 ms / 200 Hz) | px4xplane.cpp:621-650 | Heavy (work per eligible frame) | Builds one HIL_SENSOR per eligible frame. Contains:
  - `getFloat` for `g_axil`, `g_side`, `g_nrml` (MAVLinkManager.cpp:151-153) – 3 string lookups.
  - `AccelCalibration::applyCalibration(...)` (MAVLinkManager.cpp:172) – cheap after calibration, but on the ground it pushes samples into a deque until `CALIBRATION_SAMPLES`.
  - Optional vibration noise path (default `vibration_noise_enabled = true`) calls `getFloat(groundspeed)` and adds noise if moving (MAVLinkManager.cpp:199-211).
  - `applyFilteringIfNeeded` ×3 (MAVLinkManager.cpp:280-294). Default `filter_accel_enabled = false`, so just returns raw. If enabled, adds low-pass + median filter over a sliding window.
  - `setGyroData`: 3× `getFloat` for `Prad/Qrad/Rrad` + noise (MAVLinkManager.cpp:1088-1095).
  - `setPressureData`: `getFloat(elevation)`, `getFloat(indicated_airspeed)`, fresh Gaussian sample, ISA pressure, dynamic pressure (MAVLinkManager.cpp:1232-1408).
  - `setMagneticFieldData`: reads cached `earthMagneticFieldNED`, `getFloatArray("sim/flightmodel/position/q")` (string lookup + array size + array fetch), quaternion rotation, 3 noise samples (MAVLinkManager.cpp:1430-1470).
  - `getFloat("sim/cockpit2/temperature/outside_air_temp_degc")` (MAVLinkManager.cpp:402).
  - Encode + `ConnectionManager::sendData` non-blocking send loop (ConnectionManager.cpp:741-831).
  - Sparse `[RATE]` / `[TIMESTAMP_SUMMARY]` / `[SENSOR_DETAIL]` logs every 1000–10000 messages. Not per-frame in default config. |
| `MAVLinkManager::sendHILGPS()` (eligible every `TARGET_GPS_PERIOD` ≈ 100 ms / 10 Hz) | px4xplane.cpp:653-657 | Heavy (work per eligible frame) | Builds HIL_GPS. Contains:
  - `setGPSPositionData`: 3× `getFloat` lat/lon/elevation + Gaussian alt noise (MAVLinkManager.cpp:1504-1522).
  - `setGPSVelocityDataOGL`: 3× `getFloat` `local_vx/vy/vz` (MAVLinkManager.cpp:1574-1576).
  - `updateMagneticFieldIfExceededTreshold(...)`: 3× `getFloat` lat/lon/elevation, distance check; if distance > 100 m it calls `geomag::GeoMag(..., WMM2025)` (MAVLinkManager.cpp:634-653 → DataRefManager.cpp:342).
  - Encode + sendData. |
| `MAVLinkManager::sendHILStateQuaternion()` (eligible every `TARGET_STATE_QUAT_PERIOD` ≈ 20 ms / 50 Hz) | px4xplane.cpp:660-664 | Moderate | `populateHILStateQuaternion` does `getFloatArray("sim/flightmodel/position/q")`, then ~10 additional `getFloat` calls for rates, position, velocities, airspeed, and reuses `computeAcceleration()` (cache hit within the same timestamp) (MAVLinkManager.cpp:712-781). |
| `MAVLinkManager::sendHILRCInputs()` (eligible every `TARGET_RC_PERIOD` ≈ 20 ms / 50 Hz) | px4xplane.cpp:667-671 | Cheap | 4× `getFloat` joystick/throttle + mapping + encode/send (MAVLinkManager.cpp:825-847). |
| `ConnectionManager::receiveData()` | px4xplane.cpp:675 | Syscall + parser (cheap) | One `select()` + one `recv()` up to 255 bytes + `mavlink_parse_char` loop. No inner drain loop. The 255-byte cap and single recv are the key pacing/lockstep concerns (see Q4/Q5). |
| `DataRefManager::overrideActuators()` | px4xplane.cpp:678 | Moderate (airframe-dependent) | Iterates `ConfigManager::actuatorConfigs`; for each configured actuator/dataref does `XPLMFindDataRef(...)` then `XPLMSetDataf`/`XPLMSetDatavf` (DataRefManager.cpp:705-718). Then `checkAndApplyPropBrakes()` does another `XPLMFindDataRef` + `XPLMGetDatavf` over 8 throttle slots (DataRefManager.cpp:897-908). Cost scales with number of configured actuators; for a small multirotor it is modest. |
| `DataRefManager::SIM_Timestep = inElapsedSinceLastCall` | px4xplane.cpp:681 | Cheap | Float store. |

### Key cost observations

1. **String-keyed dataref lookups are not cached.** Every `DataRefManager::getFloat/getFloatArray/getInt/getDouble` call resolves the name through `XPLMFindDataRef` (DataRefManager.cpp:485, 499, 513, 528). In a single typical frame this happens dozens of times across sensor, GPS, state, RC, and actuator paths. `px4xplane.cpp:613` adds another uncached lookup for `sim/time/total_flight_time_sec`.
2. **No operation in the default path is obviously a 35 ms stall.** The heaviest single call is `geomag::GeoMag(..., WMM2025)`, but it is gated by a 100 m position threshold and is therefore not per-frame.
3. **Logging is sparse in default config.** `XPLMDebugString` / `debugLog` are not emitted per frame unless `debug_log_sensor_timing`, `debug_log_sensor_values`, or `debug_log_accel_pipeline` are enabled. They are unlikely to be the slow-frame mode by themselves.
4. **The callback is render-synchronized.** Returning `-1.0f` (px4xplane.cpp:688) tells X-Plane to call the function every frame. Consequently, the callback interval distribution is at least partially the X-Plane frame-time distribution.

---

## Exact answers to the 8 questions

### Q1. Is `XPLMFindDataRef` happening per frame instead of being cached?

**Yes.** Every `DataRefManager::getFloat`, `getDouble`, `getInt`, and `getFloatArray` call performs an uncached `XPLMFindDataRef`:

* `DataRefManager.cpp:485` (`getFloat`)
* `DataRefManager.cpp:499` (`getDouble`)
* `DataRefManager.cpp:513` (`getInt`)
* `DataRefManager.cpp:528` (`getFloatArray`)

In the flight-loop path this affects:

* `MAVLinkManager::sendHILSensor` / `computeAcceleration` (gload sensors, groundspeed, temperature, mag heading log).
* `MAVLinkManager::sendHILGPS` and `updateMagneticFieldIfExceededTreshold` (lat/lon/elevation).
* `MAVLinkManager::sendHILStateQuaternion` and `sendHILRCInputs`.
* `DataRefManager::overrideActuators()` (each configured actuator dataref).
* `DataRefManager::checkAndApplyPropBrakes()` (`ENGN_thro_use`).
* `px4xplane.cpp:613` directly for `sim/time/total_flight_time_sec`.

There is no dataref cache; all string lookups are per-frame.

### Q2. Is the WMM2025 / mag update heavy enough to stall a frame? How often is it actually invoked?

`geomag::GeoMag(decimalYear, ecefPosition, geomag::WMM2025)` is invoked from `DataRefManager::updateEarthMagneticFieldNED` at `DataRefManager.cpp:342`. It is **not** invoked every frame. It is gated by `updateMagneticFieldIfExceededTreshold` (`MAVLinkManager.cpp:634-653`), which recomputes only when the aircraft has moved more than `DataRefManager::UPDATE_THRESHOLD` meters from the last updated position (`include/DataRefManager.h:102` sets the threshold to **100 m**).

At typical SITL speeds the mag update fires every few seconds, not every 10 ms. Therefore it is **unlikely to be the source of the ~35 ms slow-frame mode**, although a profile should confirm its absolute cost because the XYZgeomag implementation is a full spherical-harmonics evaluation.

### Q3. Is logging frequent/expensive enough to create the slow-frame mode?

**No, in the default configuration.** The only unconditional log in the connected flight loop is the `[RATE]` line every 1000 sensor messages (`px4xplane.cpp:634-645`, ~once every 5 s at 200 Hz) plus first-frame logs. The `[TIMESTAMP_SUMMARY]` log in `MAVLinkManager.cpp:423-472` is also every 1000 messages.

Conditional logs controlled by `debug_log_sensor_timing`, `debug_log_sensor_values`, and `debug_log_accel_pipeline` are all **disabled by default** (`ConfigManager.cpp:127-130`). If a user enables them, log volume increases and could add millisecond-level stalls, but they are not the default cause.

### Q4. Does `receiveData` draining only `sizeof(buffer)-1` bytes per frame risk a lockstep backlog?

**The buffer is 256 bytes and `receiveData` drains at most 255 bytes per frame and does not loop until drained.**

* Buffer declared at `ConnectionManager.cpp:837`: `uint8_t buffer[256];`
* `recv` length at `ConnectionManager.cpp:860`: `sizeof(buffer) - 1` = 255 bytes.
* The function calls `select()` once, and if the socket is readable calls `recv()` once (`ConnectionManager.cpp:849-878`). There is **no `while (FD_ISSET(...))` or `while (bytesReceived == 255)` loop**.

Consequences:

* If the PX4 TCP peer ever has more than 255 bytes queued, the remainder stays in the kernel until the next flight-loop callback.
* In normal lockstep, PX4 sends one `HIL_ACTUATOR_CONTROLS` per round trip (~63 bytes after MAVLink framing), so 255 bytes is usually sufficient for several rounds.
* The lack of a drain loop is therefore a **latent backlog risk**, not a confirmed active backlog under nominal conditions.

### Q5. Does `receiveData()` splitting MAVLink frames across callbacks cause parser state churn, dropped parsed messages, or lockstep round-trip delay?

**Yes, the current parser is vulnerable to frame-splitting across callbacks.**

`MAVLinkManager::receiveHILActuatorControls` (`MAVLinkManager.cpp:877-887`) declares `mavlink_status_t status;` as a stack-local variable and zero-initializes it implicitly via value initialization every time the function is called. The parser state is only preserved **within** one `recv` buffer:

```cpp
for (int i = 0; i < size; ++i) {
    if (mavlink_parse_char(MAVLINK_COMM_0, buffer[i], &msg, &status)) {
        handleReceivedMessage(msg);
    }
}
```

If a MAVLink frame is split across two flight-loop callbacks (e.g., TCP delivers 30 bytes in callback *N* and the remaining 33 bytes in callback *N+1*):

1. **Parser state churn:** callback *N+1* starts with a zeroed `status`, so the trailing bytes from the previous frame are not resumed.
2. **Dropped parsed messages:** the bytes already consumed in callback *N* have no durable parser context; on callback *N+1* they are gone. The trailing bytes will be parsed from a zero state and will fail until a new start-of-frame (`0xFE`/`0xFD`) is seen.
3. **Lockstep round-trip delay:** PX4 emits one `HIL_ACTUATOR_CONTROLS` per received `HIL_SENSOR`. If that actuator message is dropped because of a split, the actuator values are not updated for that frame. The next complete frame arrives after at least one more callback, adding one callback interval (potentially ~35 ms) of control latency.

**Mitigation note:** This review is audit-only; adding a static parser state, a larger buffer, or a drain loop is a behavioral change and is out of scope here.

### Q6. Are actuator overrides expensive enough to explain ~35 ms frames?

**Unlikely for typical aircraft.** `DataRefManager::overrideActuators()` (`DataRefManager.cpp:692-731`) iterates over `ConfigManager::actuatorConfigs` and, for each configured actuator, does an `XPLMFindDataRef` plus one `XPLMSetDataf` or one `XPLMSetDatavf` per array index. It then calls `checkAndApplyPropBrakes()` (`DataRefManager.cpp:897-908`), which reads one array dataref and scans 8 motors.

For a small multirotor with ≤16 actuators the total SDK call count is modest. Unless the aircraft config maps many custom datarefs or `XPLMSetDatavf` triggers expensive X-Plane physics recomputation, this path is not a plausible 35 ms stall. It should be timed in the [DIAG] run to confirm.

### Q7. Is the callback rate render-bound, plugin-bound, or environment-bound?

**The callback is at least render-bound by construction.** Returning `-1.0f` (px4xplane.cpp:688) makes the callback fire once per X-Plane rendered frame. Therefore `inElapsedSinceLastCall` cannot be smaller than the X-Plane frame interval. Whether the observed bimodality (fast vs. ~35 ms frames) is:

* **render-bound** – X-Plane itself is producing a bimodal frame-time distribution (e.g., VSync, scene load, macOS window server stalls);
* **plugin-bound** – one of the per-frame operations above occasionally stalls the same frame; or
* **environment-bound** – TCP round-trip / kernel scheduling / macOS timer coalescing adds jitter,

is **not yet decidable from code inspection alone.** The deciding evidence is listed in the instrumentation spec below.

### Q8. What is the minimal read-only instrumentation to separate callback wall-time vs. render FPS vs. per-section plugin workload?

Add local, high-resolution timers inside `MyFlightLoopCallback` (no static state that survives across callbacks, no behavioral changes). Suggested sections to time:

1. Connection/HUD block (`px4xplane.cpp:553-601`).
2. Clock/timestamp advance (`px4xplane.cpp:611-614`).
3. `sendHILSensor(0)` (`px4xplane.cpp:621-650`).
4. `sendHILGPS()` (`px4xplane.cpp:653-657`).
5. `sendHILStateQuaternion()` (`px4xplane.cpp:660-664`).
6. `sendHILRCInputs()` (`px4xplane.cpp:667-671`).
7. `ConnectionManager::receiveData()` (`px4xplane.cpp:675`), including sub-timers for `select`, `recv`, and parse.
8. `DataRefManager::overrideActuators()` (`px4xplane.cpp:678`).
9. Total callback wall time (`inElapsedSinceLastCall` is already passed in).

For each section, accumulate a fixed histogram with the same buckets used by `TimestampProvider` (`<1, 1-5, 5-15, 15-25, 25-35, 35-45, 45-60, 60-100, >100` µs or ms as appropriate). Use an in-memory circular buffer; do **not** log every frame. Emit a summary line every 1000 sensor frames under a new tag, e.g. `[DIAG_FLIGHTLOOP]`, containing:

* callback counter,
* `inElapsedSinceLastCall` histogram,
* X-Plane `frame_rate_period` (read once per callback and cached in a local static, or read from `sim/operation/misc/frame_rate_period`),
* per-section max/mean/histogram,
* paused state.

For `receiveData`, additionally record (read-only):

* `select()` result (0 / readable / error),
* `bytesReceived`,
* number of complete MAVLink messages parsed in that callback,
* whether parsing ended in an incomplete state (`status.parse_state != MAVLINK_PARSE_STATE_IDLE` at end of buffer) – this flags frame-splitting without changing parser state,
* a second `select()` after `recv()` to detect undrained kernel backlog (boolean or approximate `FIONREAD`).

Correlate slow frames (callback dt > 25 ms) with:

* X-Plane `frame_rate_period` – if they track 1:1, the cause is render-bound.
* a specific section timer – if one section dominates, the cause is plugin-bound.
* `receiveData` select/recv patterns – if slow frames coincide with large `bytesReceived`, undrained backlog, or incomplete parser state, the cause is environment/transport-bound.

No sensor values, noise, calibration, PX4 params, or lockstep behavior may be changed.

---

## Ranked hypotheses for the jitter

1. **Render-bound bimodal frame pacing (most likely).** `MyFlightLoopCallback` is called every X-Plane frame. X-Plane 12 on macOS commonly shows a fast-frame / slow-frame pattern from VSync, scene complexity, or window-server timing. The ~35 ms mode (~28-30 Hz) is consistent with a missed VSync or a backgrounded/disconnected session averaged into the bundle.
2. **Per-frame string-keyed dataref lookup overhead.** Dozens of uncached `XPLMFindDataRef` calls per frame add deterministic latency. While each lookup is normally fast, on a slow or contended X-Plane/plugin path they could contribute to frame-time variance. This is a low-cost, high-confidence suspect because the lookups are provably uncached.
3. **MAVLink frame-splitting / recv-drain behavior.** The 255-byte single-recv path with a stack-local parser state can drop or delay actuator messages and may leave bytes in the kernel. This is more likely to cause lockstep latency spikes than a 35 ms stall, but it can amplify perceived jitter.
4. **WMM2025 magnetic-field stall (low probability).** `GeoMag` is expensive but fires only after 100 m of movement. It could produce a one-off slow frame when it fires, but it cannot explain a recurring ~35 ms mode.
5. **Actuator override cost (airframe-dependent, low probability).** For aircraft with many custom datarefs this could be measurable, but for the typical configs it is unlikely to be the primary 35 ms source.
6. **Debug logging stall (conditional).** Only relevant if `debug_log_*` flags are enabled; default off.

---

## Done criteria check

* [x] Every per-frame operation in `MyFlightLoopCallback` classified by cost (cached / cheap / heavy / per-frame-string-lookup / syscall / config-dependent).
* [x] Render-bound vs. plugin-bound vs. environment-bound question answered in principle, with the exact read-only instrumentation that will decide it specified.
* [x] recv-drain / 255-byte question resolved: buffer is 256 bytes, recv length 255, no drain loop; MAVLink frame-splitting resets parser state each callback and can drop/delay messages.

---

## Cross-review sign-off (Claude)

Cross-reviewer: Claude (forbidden-change-boundary enforcement + code-claim verification, the role
Issue #8 assigns me — "found a cost must not slide into implement optimization / add catch-up").

### (1) Assumptions challenged

| Runner claim | Verdict | Verification |
|---|---|---|
| Q1: `getFloat/getDouble/getInt/getFloatArray` are uncached, call `XPLMFindDataRef` every invocation | **Confirmed** | `DataRefManager.cpp:484-489` (`getFloat`), `:497-501` (`getDouble`), `:511-515` (`getInt`), `:526-534` (`getFloatArray`) — each resolves the name fresh, no cache. Hypothesis #2 stands. |
| Q4: buffer is 256, recv length 255, single select+recv, no drain loop | **Confirmed** | `ConnectionManager.cpp:837` `uint8_t buffer[256]`; `:860` `recv(..., sizeof(buffer)-1, ...)`; one `select()` (`:849`) + one `recv()`, no `while` drain. |
| Q2: WMM2025 `geomag::GeoMag` is gated by a 100 m move threshold, not per-frame | **Confirmed (plausible)** | Gating via `updateMagneticFieldIfExceededTreshold`; threshold cited `DataRefManager.h:102`. Correctly demoted to low-probability. |
| Q3: per-frame logging is sparse by default | **Confirmed** | Unconditional logs are every-1000-msg `[RATE]`/`[TIMESTAMP_SUMMARY]`; debug logs default-off. |
| Q7: callback is render-bound by construction (returns `-1.0f`) | **Confirmed** | `px4xplane.cpp:688` returns `-1.0f` → once-per-frame. Correctly framed as "at least render-bound; bimodality not decidable from code alone." |
| **Q5: stack-local `mavlink_status_t status` causes parser-state churn + dropped messages on frame-split** | **REFUTED (this is the one substantive error)** | See below. |

### (2) The Q5 correction — parser state is NOT lost across callbacks

The artifact's Q5 claims #1 (parser churn) and #2 (dropped messages) rest on the premise that the
stack-local `mavlink_status_t status` / `mavlink_message_t msg`
(`MAVLinkManager.cpp:880-881`) being re-declared each call discards partial-frame state. **That
premise is wrong.** `mavlink_parse_char(MAVLINK_COMM_0, ...)` (`:883`) keys all working parser
state on the **channel** `MAVLINK_COMM_0`: the MAVLink C library holds per-channel static buffers
(`mavlink_get_channel_status(chan)` / `mavlink_get_channel_buffer(chan)`). The passed `&msg`/
`&status` are **output parameters**, written only when a *complete* message is decoded; they carry
no continuity role. A frame split across recv calls (30 bytes in callback N, 33 in N+1) **is
correctly resumed** from the channel's retained partial state.

Decisive corroboration in this very codebase: `MAVLinkManager::reset()` calls
`mavlink_reset_channel_status(MAVLINK_COMM_0)` (`MAVLinkManager.cpp:992`). That call only exists
because channel parser state **persists across `receiveHILActuatorControls` invocations** — there
would be nothing to reset otherwise. So frame-splitting does **not** churn the parser or drop
messages.

**What survives of Q5/Q4 (reframed):** the real lockstep concern is the **undrained 255-byte
single recv** (Q4), not parser-state loss. If PX4 ever queues >255 bytes, the remainder waits in
the kernel until the next callback — up to ~one callback interval of added round-trip latency. The
mechanism is recv *throughput limiting*, not parser corruption. Under nominal lockstep (one ~63-byte
`HIL_ACTUATOR_CONTROLS` per round) 255 bytes covers several frames, so this is a **latent** risk,
matching the artifact's own Q4 wording. Recommend: rewrite Q5 to drop "parser churn / dropped
messages," keep the undrained-backlog latency point, and demote hypothesis #3 accordingly.

### (3) Forbidden-change boundary intact? **Y.**
Audit-only. No loop restructure, no optimization, no catch-up/multi-send/async, no sensor/param/
calibration change. The Q8 instrumentation spec is read-only (local timers, in-memory histograms,
a new `[DIAG_FLIGHTLOOP]` summary every 1000 frames, `status.parse_state` *read* to flag splits
without altering parser state) and §137/§191 explicitly fence mitigations as out of scope. Note a
welcome consequence of the Q5 correction: it **removes a phantom optimization** — the artifact's
§137 floats "adding a static parser state" as a hypothetical fix, but since the channel already
retains state, that change would be unnecessary as well as out of scope. Good guardrail outcome:
the supposed cost doesn't exist, so no optimization is warranted there.

### (4) Gaps the runner should note
- The Q8 read-only split-detection (`status.parse_state != MAVLINK_PARSE_STATE_IDLE` at end of
  buffer, plus a post-recv `FIONREAD`/second `select`) is exactly the right measurement — keep it;
  it will quantify undrained backlog, which is the *real* Q5 question, even after the parser-churn
  claim is dropped.
- Hypothesis ranking is otherwise sound: render-bound bimodality (#1) is the right lead given the
  `-1.0f` registration and the SD≈65%-of-mean signature; the instrumentation correctly proposes to
  correlate slow frames against `frame_rate_period` to settle render- vs plugin- vs environment-bound.

**Cross-review verdict: ACCEPT-WITH-NOTES** — cost inventory, Q1–Q4, Q6–Q8 are accurate and the
boundary is clean, but **Q5 must be corrected**: MAVLink per-channel state (proven by
`mavlink_reset_channel_status(MAVLINK_COMM_0)` at `MAVLinkManager.cpp:992`) means frame-splitting
does not churn the parser or drop messages; the surviving concern is undrained-recv lockstep
latency, not parser corruption. This sharpens — not weakens — the handoff to Issue E.
