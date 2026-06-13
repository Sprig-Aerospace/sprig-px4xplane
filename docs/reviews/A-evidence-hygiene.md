# A — HITL Evidence Hygiene, Session Partitioning & Honest Metrics

**Issue:** #6 · **Runner:** Claude · **Cross-review:** Kimi · **Status:** ready for PR review
(this is the evidence contract B/C/D are held against)
**Scope:** diagnostic & reporting code paths ONLY. No behavioral/PX4-param changes. Spec, not patch.

---

## 0. TL;DR (the contract in one breath)

Today's bundle parses the **entire** X-Plane `Log.txt` with **no session boundary**, averages
metrics across **all** PX4 connect/disconnect cycles in that file, reports a **hardcoded
`drift=0`**, computes `[RATE] avg` as a **mean-of-reciprocals (mean of 1/dt)** that overstates
rate on jitter, and **copies PX4 console output verbatim** with no current-vs-historical
disambiguation and no fixed-offset capture. The readiness self-contradiction (summary "no
failsafe" vs detail "failsafe YES") originates in that last gap: PX4's own latched/historical
validator state is pasted next to a current commander summary with nothing to tell them apart.
**Until the contract below is honored, no metric in the bundle is trustworthy for
current-readiness conclusions.**

---

## 1. Evidence-field trust table

| Evidence field | Emitted at | Carries session id (generation)? | Current or historical? | Trustworthy today? | Reason |
|---|---|---|---|---|---|
| `[TRANSPORT_EVENT]` JSON | `ConnectionManager.cpp:258` via `buildTransportSessionEventJson` (`:225`) | **YES** — `"generation"` field | Current at emit; but parser keeps **all** generations | **Partial** | The only line with a session id. Parser (`parse_log`) never filters by latest generation, so old-generation events are mixed into `transport_events`, `estimated_fps`. |
| `[RATE]` line | `px4xplane.cpp:642` | **NO** | Cumulative within a session (1000-msg window), no session tag | **NO** | No generation; `avg` is mean-of-reciprocals (§3); `sim_time` is plugin-clock seconds, not wall time; parser regex doesn't even match this format (§5). |
| `[TIMESTAMP]` lines (init/branch/reset) | `TimestampProvider.cpp:32/88/164` | **NO** | Current step-clock state | **NO** for drift | `drift` is hardcoded `0` (§4). `step_clock` is the accumulated callback delta, not measured against any wall clock. No generation. |
| `HIL_SENSOR: N msgs, avg X Hz` | **not emitted anywhere in current source** | n/a | n/a | **NO (phantom)** | `HIL_SENSOR_RATE_RE` (`bundle:36`) matches a legacy format that the code no longer produces (§5). Any "HIL_SENSOR send-rate mean" the summary prints is from stale/legacy logs only. |
| `MAVLink rates - SENSOR:..` / `Message periods initialized` | px4xplane init (config echo) | **NO** | Config-at-init (static) | **Partial** | Fine as config provenance; `latest_rates = mavlink_rates[-1]` can pick a value from an **earlier** session in a multi-session file. |
| Resolved config path / `Config Name` | init | **NO** | Config-at-init | **Partial** | Provenance only; `dict.fromkeys` dedups but spans all sessions. |
| `estimated_fps` (from TRANSPORT_EVENT and bare `FPS_RE`) | `:248` and `bundle:54` | TRANSPORT_EVENT: yes; bare `"estimated_fps":` regex: **NO** | Current at emit | **NO (aggregated)** | `fps_mean`/`fps_min` are means over **every** session in the file; one bad backgrounded session poisons the mean. |
| `transport_alerts` (backpressure/retry/broken pipe/drop) | `:778/:822` + text matches | backpressure/retry carry generation (TRANSPORT_EVENT); text matches don't | Historical (count of all occurrences) | **Partial** | Counted across the whole file; a count of `N` cannot be attributed to the current session. |
| PX4 validator/failsafe status, EKF2 updates | **PX4 console**, pasted into `px4_output.txt`, `shutil.copy2` at `:335` | **NO** | **Unknown — could be latched/historical** | **NO** | Root of the self-contradiction (§6). Bundle copies verbatim; never reconciles summary vs detail, never timestamps the capture. |

---

## 2. Q1–Q3 — Session boundary (the central defect)

**Q1. Are we parsing stale lines from prior sessions? Where does the parser establish the
"current session" boundary?**
It does **not**. `parse_log` (`bundle:124–196`) opens `Log.txt` and iterates **every line from
the top of the file to EOF**, appending each match into flat lists with no notion of a session.
There is no marker scan, no "seek to last `session_reset`", no generation filter. X-Plane keeps a
single `Log.txt` for the whole X-Plane run, and a single run can contain **many** PX4
connect → disconnect → reconnect cycles (each bumps `generation` at
`ConnectionManager.cpp:526` and emits `client_connected`+`session_reset` at `:538/:550`). All of
those cycles are flattened together.

**Q2. Does every evidence line carry a session generation/id?**
- **Has it:** `[TRANSPORT_EVENT]` (`"generation"`), and by extension `consecutive_backpressure/retry`.
- **Lacks it:** `[RATE]`, `[TIMESTAMP]` (all variants), the config-echo lines, the bare
  `estimated_fps` regex path, and the entire PX4 console paste.

**Q3. Does readiness only consider lines emitted after the latest `session_reset`?**
**No.** Every summarized metric (`sensor_rate_mean`, `fps_mean`, `fps_min`, `latest_rates`,
counts) is computed over the full-file lists (`render_summary`, `bundle:218–257`). A clean
current session's numbers are diluted (or replaced) by stale ones.

> **Worst case:** operator runs once with X-Plane backgrounded (bad FPS), fixes it, reconnects,
> runs clean. The bundle's `fps_mean`/`rate_mean` blend both; the "current readiness" picture is
> a lie by averaging.

---

## 3. Q5 — Is `[RATE]` count/wall-time or mean-of-reciprocals?

**Mean-of-reciprocals (mean of 1/dt). Overstates rate on jitter.** Exact computation,
`px4xplane.cpp:627–635`:

```cpp
double actualDt_sec = currentSimTime - lastSensorSendTime;
if (actualDt_sec > 0.0) {
    float actualRate = static_cast<float>(1.0 / actualDt_sec);   // <-- reciprocal per sample
    sensorRateSum += actualRate;
}
...
float avgRate = sensorRateSum / 1000.0f;                          // <-- mean of reciprocals
```

By Jensen's inequality, `mean(1/dt) ≥ 1/mean(dt)`; the gap grows with dt variance. With the
bimodal fast/slow-frame distribution Issue C is chasing (SD ≈ 65 % of mean), the reported `avg`
Hz is **biased high** exactly when cadence is worst. `currentSimTime` is the plugin clock
(`pluginClockSec`, accumulated `inElapsedSinceLastCall`, `:611`), so even the time base is
callback-derived, not wall.

**Honest definition for the contract:** `RATE = (message count) / (wall-clock seconds elapsed
over the same window)`. Report it alongside, never instead of, the dt histogram
(`TimestampProvider` already keeps a true per-kind dt histogram at `:130–151` — that is the
trustworthy primitive; the scalar "avg Hz" is not).

---

## 4. Q6 — Is `drift_ms` measured or hardcoded?

**Hardcoded to zero. It measures nothing.** `TimestampProvider::s_diagnostics.drift_usec` is set
to literal `0` at every branch — `:50` (first call), `:76` (every subsequent advance) — and never
assigned any other value. `getDiagnostics` returns that zero (`:171`), and the `[TIMESTAMP]` line
prints `drift=%+.3f ms` from `drift_usec/1000.0` (`:89,96`) → **always `+0.000ms`**.

Root reason it's structurally meaningless: the simulation clock is **defined** as the running sum
of `inElapsedSinceLastCall` (`advanceSimulationClock`, `:73`). There is no independent wall-clock
reference captured to diff against, so "drift vs wall clock" is not just unmeasured — there is
nothing in the code that could be nonzero. `drift_ms=0.000` is an artifact, not a measurement.

**Honest definition for the contract:** drift MUST be `step_clock_usec − (wall_now_usec −
wall_session_start_usec)`, where `wall_*` come from `TimeManager::getCurrentTimeUsec()`
(`SteadyClock`, independent of the accumulated callback sum). If we don't sample a real wall
clock, the field must be emitted as `drift=unmeasured`, never `0`.

---

## 5. Emitter ⇄ parser format drift (a second-order hygiene bug)

Two parser regexes match formats the **current** code does not emit:

- `HIL_SENSOR_RATE_RE` (`bundle:36`) expects `HIL_SENSOR: N msgs, avg X Hz (target N Hz)`.
  Current code emits `[RATE] sim_time=.. HIL_SENSOR msgs=.. avg=..Hz target=..Hz ..`
  (`px4xplane.cpp:642`). **No match** → `hil_sensor_rates_hz` populates only from legacy logs.
- `EFFECTIVE_RATE_RE` (`bundle:39`) expects `[RATE] ... achieved Hz SENSOR:.. X-Plane:.. FPS`.
  Current `[RATE]` line has no "achieved Hz/X-Plane FPS" tokens. **No match.**

Consequence: the bundle's self-test (`bundle:362–390`) feeds it **synthetic legacy lines**, so it
passes green while being blind to real current output. Any "HIL_SENSOR send-rate mean" or
effective-rate number in a real bundle is sourced from stale log fragments or is absent. The
contract must require **emitter and parser to share one versioned line grammar**.

---

## 6. Q4 — The readiness self-contradiction, root-caused

**Q4. Are reported validator failsafes current-state or cumulative/historical? Can a cleared
failsafe still show "YES"?**

The plugin emits **no** failsafe/validator text — `grep` over `src/` finds the strings only as
**checklist labels** in `bundle.py:277–278`. The "no selected-sensor failsafe" vs "accel/gyro
validator failsafe: YES" contradiction therefore lives entirely in **PX4 console output**, which
the bundle ingests by `shutil.copy2(px4_output, ...)` (`bundle:335`) — a verbatim copy, never
parsed, never reconciled.

Two PX4 surfaces disagree by design:
- the **commander preflight summary** reflects *current* arming gating, and
- a **sensor-validator / `px4-sensors status` detail** line can report a **latched or
  historical** validator trip that fired during the startup window and was never cleared in the
  text.

Because the bundle (a) imposes no session boundary and (b) captures `px4_output` as a **single
undated operator paste**, a validator that tripped early and cleared is indistinguishable from one
that is currently failed. **Root cause (in scope):** the bundle has *no temporal structure and no
current-vs-historical tagging for PX4 capture* — it cannot answer "is this YES current?" so two
PX4 lines from different moments are presented as equally authoritative.

This is the concrete reason the issue says "until evidence is trustworthy, no other review's
conclusions can be trusted": D's "latched failsafe" and B's "EKF2 zero updates" both read the
same un-partitioned, un-dated PX4 paste.

---

## 7. Q7 — Fixed-offset PX4 capture (connect / +5 s / +30 s)

**Does the bundle capture PX4 output at fixed offsets so transient vs persistent failures
separate?** **No.** `--px4-output` is a single optional text file the operator pastes once
(`bundle:334–335`, `409`); there is no timed capture, no `t0/t5/t30` structure.

**Spec to enable it (capture-script change only, no runtime change):** add a `--px4-capture`
mode that, given a way to issue PX4 shell commands (e.g. a MAVLink shell / `pxh` pipe the operator
points at), runs the existing command list (`bundle:284–295`) **three times** — at connect (t0),
t0+5 s, t0+30 s — writing `px4_t0.txt`, `px4_t5.txt`, `px4_t30.txt`, plus a diff. A validator that
is YES at t0 but absent at t30 = **transient/latched-then-cleared**; YES at all three =
**persistent**. This is the minimal change; it stays read-only (only runs `listener`/`param
show`/`ekf2 status`, all observational).

---

## 8. THE EVIDENCE CONTRACT (spec only — hand to B/C/D)

### 8.1 Session-boundary rule (mandatory)
> **Only evidence emitted after the latest `client_connected` + `session_reset` pair may be used
> for current-readiness conclusions.** Concretely: the parser MUST scan for the **highest
> `generation`** seen in `[TRANSPORT_EVENT]` lines, find the `client_connected` then
> `session_reset` for that generation, and **discard every matched line before that point** from
> all current-readiness aggregates. Pre-boundary lines may be retained only in a clearly labeled
> `historical/` section, never folded into means/counts presented as "current".

### 8.2 Required fields on every evidence line
Every diagnostic line that feeds a readiness conclusion MUST carry:
1. `generation` (the transport session id, source of truth `g_transportSessionState.generation`),
2. a wall-clock timestamp (`TimeManager::getCurrentTimeUsec`), and
3. a versioned line tag so emitter/parser grammars can't silently drift (§5).

`[RATE]` and `[TIMESTAMP]` lines do **not** carry `generation` today and MUST before their numbers
are trusted.

### 8.3 Honest-metric definitions
- **RATE** = `message_count / wall_clock_window_seconds` (count-over-wall-time). The
  mean-of-reciprocals scalar is **banned** from readiness output. Always publish the
  `TimestampProvider` dt histogram beside any rate scalar.
- **drift** = `step_clock_usec − (wall_now − wall_session_start)` using an independent wall clock;
  if not sampled, emit `unmeasured`. A literal `0` is **non-conformant**.
- **FPS** reported as current-session mean/min only (post-boundary), never whole-file mean.

### 8.4 PX4 current-vs-historical rule
PX4 validator/failsafe/EKF2 lines MUST be tagged with capture offset (t0/t5/t30, §7). A "YES"
without a current-offset capture is **historical and non-decisive**. The bundle must stop
presenting a single undated paste as current state.

### 8.5 Emitter/parser parity
One versioned grammar for `[RATE]`, `[TIMESTAMP]`, `[TRANSPORT_EVENT]`. The self-test must feed
**real current-format** lines (not legacy synthetic), or it certifies nothing.

---

### 8.6 Boundary-rule hardening (folded in from Kimi cross-review)
The §8.1 rule is tightened by these mandatory clarifications so B/C/D can rely on it:
- **Incomplete-session fallback:** if the log ends after a `client_connected` but before its
  matching `session_reset` (highest generation), the parser MUST NOT silently discard current
  evidence — it falls back to the latest `client_connected` as the boundary and flags the bundle
  `incomplete-session` (or rejects it), never drops the live session.
- **Generation-less lines = line-order boundary:** config echoes, `[RATE]`, `[TIMESTAMP]`, and
  drop/backpressure text carry no generation; the boundary mechanism for them is **raw line
  order** — any line appearing before the `session_reset` of the highest generation is historical.
  The parser MUST preserve and use line order, not just generation fields.
- **Pre-increment events are previous-session:** `stale_client_replaced`
  (`ConnectionManager.cpp:521`) is emitted *before* the generation bump (`:526`), so it belongs to
  the prior session and MUST be classified historical despite carrying the new generation context.
- **PX4 paste needs a temporal anchor:** `--px4-output` (`bundle:334-335`) has no wall-clock tag
  binding it to the `Log.txt` timeline; the §7 t0/t5/t30 capture MUST stamp each capture with a
  wall clock so it can be mapped to the current session — offset labels alone are insufficient.
- **Count fields inherit contamination:** "Transport events" / "TimestampProvider lines" raw counts
  (`bundle:256-257`) are whole-file and MUST be marked untrustworthy alongside the means.

## 9. Where B/C/D MUST distrust today's bundle

1. **Any "mean Hz" / "send-rate mean" number** — mean-of-reciprocals and/or whole-file averaged.
   B (HIL_SENSOR rate) and C (cadence) must use the dt histogram, per-session, not this scalar.
2. **`drift_ms = 0.000`** — hardcoded; carries zero information. Nobody may cite "no drift".
3. **`estimated_fps` mean/min** — averaged across all sessions incl. backgrounded ones (C).
4. **Validator/failsafe "YES/NO"** — un-partitioned, undated PX4 paste; could be latched/historical
   (D's poisoning vs B's gating cannot be separated from this alone).
5. **`HIL_SENSOR send-rate mean` / effective-rate fields** — phantom: parser regex doesn't match
   current emission (§5); value is from legacy logs or absent (B).
6. **`latest_rates = mavlink_rates[-1]`** — can be from an earlier session in a multi-connect file.
7. **`transport_alerts` counts** — cumulative over the whole file; not attributable to the current
   session (C's recv-backlog question, D's reconnect question).
8. **Lines emitted before the latest `session_reset`** — until §8.1 is enforced, every aggregate
   includes them.

B/C/D: cite raw post-boundary `[TRANSPORT_EVENT]` (it has `generation`) and the per-kind dt
histogram; treat every scalar in `README.md` as suspect until the contract is implemented.

---

## 10. Forbidden-change boundary (self-audit)
This artifact is spec only. No fix implemented; no sensor value, noise, calibration, or PX4 param
touched; no catch-up/multi-send/async proposed. The capture-offset change (§7) and the
session-boundary parser change (§8.1) are described as **specs for a separate implementation
ticket**, not applied. ✔

---

## 10.1 PR validation note

This issue is a review/spec artifact, so it does not require a live PX4/X-Plane diagnostic run.
Validation for the PR is limited to source inspection and the existing diagnostic bundle checks:

```text
python3 scripts/hitl_diagnostic_bundle.py --self-test
python3 -m py_compile scripts/hitl_diagnostic_bundle.py
```

Both checks passed before opening the issue #6 PR.

---

## 11. Cross-review sign-off (Kimi)

### (1) Assumptions challenged

| Claim tested | Verdict | Evidence |
|---|---|---|
| "No session boundary in `parse_log`" | **Confirmed** | `scripts/hitl_diagnostic_bundle.py:129-195` iterates every line from top to bottom and appends each match into flat lists; there is no generation filter and no seek to the last `session_reset`. The loop is: `with path.open(...) as handle: for raw_line in handle: line = raw_line.rstrip("\n"); if any(pattern in line ...): evidence.excerpts.append(line); if match := TRANSPORT_EVENT_RE.search(line): ...`. |
| "`drift_usec` is hardcoded to 0" | **Confirmed** | `TimestampProvider.cpp:51` (`s_diagnostics.drift_usec = 0;`) and `:76` (`s_diagnostics.drift_usec = 0;`) are the only assignments; `resetDiagnostics()` (`:179-181`) value-initializes the struct and only sets `simulation_clock_usec`. No wall-clock diff is ever computed. |
| "`[RATE] avg` is mean-of-reciprocals" | **Confirmed** | `src/px4xplane.cpp:627-630`: `double actualDt_sec = currentSimTime - lastSensorSendTime; ... float actualRate = static_cast<float>(1.0 / actualDt_sec); sensorRateSum += actualRate;` and `:635`: `float avgRate = sensorRateSum / 1000.0f;`. |
| "Readiness self-contradiction is caused by unpartitioned PX4 console paste" | **Confirmed** | `grep -iR 'failsafe\|validator\|selected-sensor' src/` returned no matches. The plugin never emits failsafe/validator text; the strings exist only as checklist labels in `scripts/hitl_diagnostic_bundle.py:277-278`. The contradiction therefore lives entirely in the PX4 shell output copied by `shutil.copy2(px4_output, ...)` (`bundle:334-335`) with no temporal reconciliation. |
| "`HIL_SENSOR_RATE_RE` / `EFFECTIVE_RATE_RE` match legacy output only" | **Confirmed** | Current emitter `src/px4xplane.cpp:642-644` prints `px4xplane: [RATE] sim_time=%.1fs HIL_SENSOR msgs=%d avg=%.1fHz target=%dHz estimated_fps=%.1f paused=%d`; neither `bundle:36` (`HIL_SENSOR: N msgs, avg X Hz`) nor `bundle:39` (`achieved Hz SENSOR:... X-Plane:... FPS`) matches it. |
| "Transport session model bumps generation and emits `client_connected`+`session_reset`" | **Confirmed** | `src/ConnectionManager.cpp:526` increments `g_transportSessionState.generation`, `:538` emits `client_connected`, `:550` emits `session_reset`. |
| "`[RATE]` and `[TIMESTAMP]` lack generation" | **Confirmed** | No generation field is printed in `src/px4xplane.cpp:642-644` or `src/TimestampProvider.cpp:88-96`. |

### (2) Forbidden-change boundary intact?

**Y.** The artifact is diagnostic/spec-only. It does not modify `src/`, `config/`, `scripts/`, sensor values, noise, calibration, or PX4 parameters, and it does not propose catch-up/multi-send/async publishing. The capture-offset (`§7`) and session-boundary parser change (`§8.1`) are framed as specs for a separate implementation ticket.

### (3) Gaps the runner missed

- **Cross-file PX4 output has no temporal anchor.** `§8.1` correctly partitions lines inside `Log.txt`, but the `--px4-output` file (`scripts/hitl_diagnostic_bundle.py:334-335`) is copied verbatim with no wall-clock timestamp and no binding to the X-Plane log timeline. Even the proposed t0/t5/t30 capture mode cannot be mapped to "current session" without an explicit time tag.
- **The session-boundary rule is fragile to truncation/missing markers.** `§8.1` says the parser must find the `client_connected` *then* `session_reset` for the highest generation. If the log ends after `client_connected` but before `session_reset` for the current generation, the rule yields no boundary and would silently discard current-session evidence. The contract should specify a fallback (e.g., use the latest `client_connected` when no reset follows, or reject the bundle as incomplete).
- **Pre-session init lines are generation-less and pre-boundary.** Config-path echoes and MAVLink-rate lines emitted before the first `client_connected`/`session_reset` pair carry no generation. The rule's line-ordering discard handles them only if the parser preserves raw line order; the contract should explicitly state that ordering is the boundary mechanism for generation-less lines.
- **Pre-increment transport events can be mis-attributed.** `stale_client_replaced` (`src/ConnectionManager.cpp:521`) is emitted *before* the generation increment at `:526`, so it belongs to the previous session. Text matches such as "Replacing existing PX4 client..." (`:517-520`) and drop/backpressure strings (`scripts/hitl_diagnostic_bundle.py:181-189`) have no generation at all. The boundary rule should explicitly classify any line before the `session_reset` of the highest generation as historical, including events emitted between disconnect and reset.
- **One aggregate left implicitly unclassified.** `render_summary` prints raw counts for "Transport events" and "TimestampProvider lines" (`scripts/hitl_diagnostic_bundle.py:256-257`). These counts inherit the same whole-file contamination as their source lists, but the runner did not explicitly mark the *count* fields as untrustworthy.

**Cross-review verdict: ACCEPT-WITH-NOTES** — the contamination hypothesis is fully supported by the source, and the evidence contract is directionally sound, but the session-boundary rule has edge cases (truncation, cross-file PX4 output, pre-increment/pre-reset events) that should be tightened before B/C/D rely on it.
