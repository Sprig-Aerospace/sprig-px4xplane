#!/usr/bin/env python3
"""Collect px4xplane HITL cadence evidence without changing runtime behavior."""

from __future__ import annotations

import argparse
import configparser
import difflib
import hashlib
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_CONFIG = ROOT / "config" / "config.ini"
DEFAULT_XPLANE_LOG = Path.home() / "X-Plane 12" / "Log.txt"
DEFAULT_INSTALLED_CONFIG = Path.home() / "X-Plane 12" / "Resources" / "plugins" / "px4xplane" / "64" / "config.ini"
DEFAULT_OUTPUT_ROOT = ROOT / "build" / "diagnostics"

PX4XPLANE_PATTERNS = (
    "px4xplane",
    "HIL_SENSOR",
    "[TIMESTAMP]",
    "[TRANSPORT_EVENT]",
    "dropping this frame",
    "send failure",
    "broken pipe",
)

TRANSPORT_EVENT_RE = re.compile(r"\[TRANSPORT_EVENT\]\s+({.*})")
HIL_SENSOR_RATE_RE = re.compile(
    r"HIL_SENSOR:\s+(?P<count>\d+)\s+msgs,\s+avg\s+(?P<rate>[0-9.]+)\s+Hz\s+\(target\s+(?P<target>\d+)\s+Hz\)"
)
EFFECTIVE_RATE_RE = re.compile(
    r"\[RATE\].*achieved Hz SENSOR:(?P<sensor>[0-9.]+).*X-Plane:(?P<xplane_fps>[0-9.]+) FPS"
)
MESSAGE_PERIOD_RE = re.compile(
    r"Message periods initialized - SENSOR:(?P<sensor_period>[0-9.]+)s \((?P<sensor>\d+)Hz\) "
    r"GPS:(?P<gps_period>[0-9.]+)s \((?P<gps>\d+)Hz\) "
    r"STATE:(?P<state_period>[0-9.]+)s \((?P<state>\d+)Hz\) "
    r"RC:(?P<rc_period>[0-9.]+)s \((?P<rc>\d+)Hz\)"
)
MAVLINK_RATES_RE = re.compile(
    r"MAVLink rates - SENSOR:(?P<sensor>\d+)Hz GPS:(?P<gps>\d+)Hz STATE:(?P<state>\d+)Hz RC:(?P<rc>\d+)Hz"
)
CONFIG_PATH_RE = re.compile(r"(?:Resolved config file path|Loading config file from):\s+(?P<path>.+)")
CONFIG_NAME_RE = re.compile(r"Config Name:\s+(?P<name>.+)")
TIMESTAMP_RE = re.compile(r"\[TIMESTAMP\]\s+(?P<message>.+)")
FPS_RE = re.compile(r'"estimated_fps":(?P<fps>-?[0-9.]+)')

PX4_CAPTURE_OFFSETS = (("t0", 0), ("t5", 5), ("t30", 30))
PX4_CAPTURE_COMMANDS = (
    "param show IMU_INTEG_RATE",
    "param show EKF2_PREDICT_US",
    "param show EKF2_EN",
    "param show SYS_MC_EST_GROUP",
    "param show SYS_HAS_MAG",
    "param show EKF2_MAG_TYPE",
    "param show EKF2_MULTI_IMU",
    "param show SENS_IMU_MODE",
    "param show SENS_EN_GPSSIM",
    "param show SENS_EN_BAROSIM",
    "param show SENS_EN_MAGSIM",
    "listener sensor_accel 10",
    "listener sensor_gyro 10",
    "listener sensor_baro 5",
    "listener sensor_mag 5",
    "listener sensor_gps 5",
    "listener vehicle_imu 10",
    "listener vehicle_imu_status 3",
    "listener vehicle_acceleration 10",
    "listener vehicle_angular_velocity 10",
    "listener vehicle_attitude 5",
    "listener estimator_selector_status 3",
    "listener vehicle_air_data 10",
    "listener vehicle_magnetometer 10",
    "listener vehicle_gps_position 10",
    "listener differential_pressure 3",
    "listener airspeed_validated 3",
    "listener estimator_status 5",
    "listener estimator_status_flags 5",
    "listener estimator_sensor_bias 5",
    "listener vehicle_local_position 5",
    "listener vehicle_global_position 5",
    "ekf2 status",
    "sensors status",
    "px4-sensors status",
    "commander status",
    "commander check",
    "mavlink status",
    "work_queue status",
    "uorb top",
    "uorb top -1 sensor_baro",
    "uorb top -1 vehicle_gps_position",
    "uorb status",
)
PX4_GATE_READOUT = (
    ("1", "IMU present", "sensor_accel, sensor_gyro"),
    ("2", "IMU integrated/selected", "vehicle_imu, vehicle_imu_status, estimator_selector_status"),
    ("3", "IMU validator/current preflight", "sensors status, px4-sensors status, commander check"),
    ("4", "Timestamp/lockstep stall", "ekf2 status, vehicle_imu timestamps, mavlink status"),
    ("5", "Baro aiding", "vehicle_air_data, sensor_baro, uorb top -1 sensor_baro"),
    ("6", "Mag/yaw aiding", "vehicle_magnetometer, sensor_mag, SYS_HAS_MAG, EKF2_MAG_TYPE"),
    ("7", "GPS present/valid", "vehicle_gps_position, sensor_gps, uorb top -1 vehicle_gps_position"),
    ("8", "GPS origin/aiding init", "estimator_status_flags, vehicle_local_position"),
    ("9", "EKF2 updates", "ekf2 status, estimator_status, estimator_sensor_bias"),
    ("10", "Global position emitted", "vehicle_global_position"),
)


@dataclass
class LogEvidence:
    excerpts: list[str] = field(default_factory=list)
    transport_events: list[dict[str, object]] = field(default_factory=list)
    hil_sensor_rates_hz: list[float] = field(default_factory=list)
    timestamp_lines: list[str] = field(default_factory=list)
    config_paths: list[str] = field(default_factory=list)
    config_names: list[str] = field(default_factory=list)
    mavlink_rates: list[dict[str, int]] = field(default_factory=list)
    estimated_fps: list[float] = field(default_factory=list)
    transport_alerts: list[str] = field(default_factory=list)


@dataclass
class PX4Capture:
    label: str
    offset_sec: int
    source_path: Path
    output_path: Path
    wall_time_utc: str


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_text_if_exists(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig", errors="replace")


def parse_ini_without_sections(path: Path) -> dict[str, str]:
    text = read_text_if_exists(path)
    if not text:
        return {}
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read_string("[px4xplane]\n" + text)
    return dict(parser["px4xplane"])


def config_summary(path: Path) -> dict[str, str]:
    values = parse_ini_without_sections(path)
    keys = (
        "config_name",
        "mavlink_sensor_rate_hz",
        "mavlink_gps_rate_hz",
        "mavlink_state_rate_hz",
        "mavlink_rc_rate_hz",
        "debug_log_sensor_timing",
    )
    return {key: values.get(key, "") for key in keys}


def unified_config_diff(installed_config: Path) -> str:
    if not REPO_CONFIG.is_file() or not installed_config.is_file():
        return ""
    repo_lines = REPO_CONFIG.read_text(encoding="utf-8-sig", errors="replace").splitlines(keepends=True)
    installed_lines = installed_config.read_text(encoding="utf-8-sig", errors="replace").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            repo_lines,
            installed_lines,
            fromfile=str(REPO_CONFIG),
            tofile=str(installed_config),
        )
    )


def parse_log(path: Path) -> LogEvidence:
    evidence = LogEvidence()
    if not path.is_file():
        return evidence

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            if any(pattern in line for pattern in PX4XPLANE_PATTERNS):
                evidence.excerpts.append(line)

            if match := TRANSPORT_EVENT_RE.search(line):
                try:
                    event = json.loads(match.group(1))
                    evidence.transport_events.append(event)
                    fps = event.get("estimated_fps")
                    if isinstance(fps, (int, float)) and fps > 0:
                        evidence.estimated_fps.append(float(fps))
                except json.JSONDecodeError:
                    evidence.transport_alerts.append(line)

            if match := HIL_SENSOR_RATE_RE.search(line):
                evidence.hil_sensor_rates_hz.append(float(match.group("rate")))

            if match := EFFECTIVE_RATE_RE.search(line):
                evidence.hil_sensor_rates_hz.append(float(match.group("sensor")))
                evidence.estimated_fps.append(float(match.group("xplane_fps")))

            if match := MESSAGE_PERIOD_RE.search(line):
                evidence.mavlink_rates.append(
                    {
                        "sensor": int(match.group("sensor")),
                        "gps": int(match.group("gps")),
                        "state": int(match.group("state")),
                        "rc": int(match.group("rc")),
                    }
                )

            if match := MAVLINK_RATES_RE.search(line):
                evidence.mavlink_rates.append(
                    {
                        "sensor": int(match.group("sensor")),
                        "gps": int(match.group("gps")),
                        "state": int(match.group("state")),
                        "rc": int(match.group("rc")),
                    }
                )

            if match := CONFIG_PATH_RE.search(line):
                evidence.config_paths.append(match.group("path").strip())

            if match := CONFIG_NAME_RE.search(line):
                evidence.config_names.append(match.group("name").strip())

            if match := TIMESTAMP_RE.search(line):
                evidence.timestamp_lines.append(match.group("message").strip())

            lowered = line.lower()
            if (
                "send_backpressure" in line
                or "send_retry_limit" in line
                or "dropping this frame" in lowered
                or "send failure" in lowered
                or "broken pipe" in lowered
            ):
                evidence.transport_alerts.append(line)

            if "[TRANSPORT_EVENT]" not in line and (match := FPS_RE.search(line)):
                fps = float(match.group("fps"))
                if fps > 0:
                    evidence.estimated_fps.append(fps)

    return evidence


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def metric_line(label: str, value: object) -> str:
    return f"- {label}: {value if value not in (None, '') else 'not captured'}"


def utc_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def render_px4_command_sheet() -> str:
    lines = [
        "# PX4 t0/t5/t30 EKF2 Gate Capture Sheet",
        "",
        "This sheet is read-only. Do not run `param set`, change PX4 params, restart modules, or change px4xplane config while collecting this evidence.",
        "",
        "## Timing",
        "- `t0`: immediately after PX4 connects to px4xplane for the current session.",
        "- `t5`: five seconds after `t0`.",
        "- `t30`: thirty seconds after `t0`.",
        "",
        "Before each capture, record the host wall clock in UTC next to the pasted output. If the output is saved directly to files, preserve the file modification time.",
        "",
        "Suggested file names:",
        "- `px4_t0.txt`",
        "- `px4_t5.txt`",
        "- `px4_t30.txt`",
        "",
        "## Commands",
        "",
        "Run this full command set at each offset:",
        "",
        "```text",
    ]
    lines.extend(PX4_CAPTURE_COMMANDS)
    lines.extend(
        [
            "```",
            "",
            "## Gate Readout Order",
            "",
            "| Gate | Signal | Capture evidence |",
            "|---|---|---|",
        ]
    )
    for gate, signal, evidence in PX4_GATE_READOUT:
        lines.append(f"| {gate} | {signal} | {evidence} |")
    lines.extend(
        [
            "",
            "## Bundle Ingest",
            "",
            "After the run, pass the pasted files to the bundle:",
            "",
            "```bash",
            "python3 scripts/hitl_diagnostic_bundle.py \\",
            "  --px4-output-t0 /path/to/px4_t0.txt \\",
            "  --px4-output-t5 /path/to/px4_t5.txt \\",
            "  --px4-output-t30 /path/to/px4_t30.txt",
            "```",
            "",
            "A validator or failsafe `YES` line without one of these offset labels is historical/non-decisive.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_px4_captures(output_dir: Path, capture_inputs: dict[str, Path | None]) -> list[PX4Capture]:
    captures: list[PX4Capture] = []
    for label, offset_sec in PX4_CAPTURE_OFFSETS:
        source_path = capture_inputs.get(label)
        if not source_path or not source_path.is_file():
            continue

        wall_time = utc_mtime(source_path)
        output_path = output_dir / f"px4_{label}.txt"
        source_text = read_text_if_exists(source_path)
        output_path.write_text(
            "\n".join(
                [
                    f"# px4_capture_offset: {label}",
                    f"# px4_capture_offset_sec: {offset_sec}",
                    f"# px4_capture_source: {source_path}",
                    f"# px4_capture_wall_time_utc: {wall_time}",
                    "# px4_capture_command_set: issue-13-ekf2-gates-v1",
                    "",
                    source_text.rstrip("\n"),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        captures.append(
            PX4Capture(
                label=label,
                offset_sec=offset_sec,
                source_path=source_path,
                output_path=output_path,
                wall_time_utc=wall_time,
            )
        )
    return captures


def render_px4_capture_diff(captures: list[PX4Capture]) -> str:
    if len(captures) < 2:
        return "Need at least two offset captures to render a PX4 capture diff.\n"

    sections: list[str] = []
    for previous, current in zip(captures, captures[1:]):
        previous_lines = previous.output_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        current_lines = current.output_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        sections.append(f"# Diff {previous.label} -> {current.label}\n")
        sections.append(
            "".join(
                difflib.unified_diff(
                    previous_lines,
                    current_lines,
                    fromfile=previous.output_path.name,
                    tofile=current.output_path.name,
                )
            )
            or "(no textual differences)\n"
        )
        sections.append("\n")
    return "".join(sections)


def render_summary(
    *,
    output_dir: Path,
    xplane_log: Path,
    installed_config: Path,
    px4_output: Path | None,
    px4_captures: list[PX4Capture],
    evidence: LogEvidence,
    installed_hash: str | None,
    repo_hash: str | None,
    config_diff: str,
) -> str:
    installed_summary = config_summary(installed_config)
    repo_summary = config_summary(REPO_CONFIG)
    latest_rates = evidence.mavlink_rates[-1] if evidence.mavlink_rates else {}
    fps_mean = mean(evidence.estimated_fps)
    fps_min = min(evidence.estimated_fps) if evidence.estimated_fps else None
    sensor_rate_mean = mean(evidence.hil_sensor_rates_hz)

    lines = [
        "# px4xplane HITL Cadence Evidence",
        "",
        "This bundle is an evidence gate before scheduler changes. It does not prove the HITL issue is fixed.",
        "",
        "## Inputs",
        metric_line("Bundle directory", output_dir),
        metric_line("X-Plane log", xplane_log),
        metric_line("Installed config", installed_config),
        metric_line("PX4 output", px4_output or "not provided"),
        "",
        "## Config Evidence",
        metric_line("Repo config hash", repo_hash),
        metric_line("Installed config hash", installed_hash),
        metric_line("Installed config differs from repo", "yes" if config_diff else "no"),
        metric_line("Installed config_name", installed_summary.get("config_name")),
        metric_line("Repo config_name", repo_summary.get("config_name")),
        metric_line("Installed mavlink_sensor_rate_hz", installed_summary.get("mavlink_sensor_rate_hz")),
        metric_line("Installed mavlink_gps_rate_hz", installed_summary.get("mavlink_gps_rate_hz")),
        metric_line("Installed mavlink_state_rate_hz", installed_summary.get("mavlink_state_rate_hz")),
        metric_line("Installed mavlink_rc_rate_hz", installed_summary.get("mavlink_rc_rate_hz")),
        metric_line("Installed debug_log_sensor_timing", installed_summary.get("debug_log_sensor_timing")),
        "",
        "## Log Evidence",
        metric_line("Resolved config paths in log", ", ".join(dict.fromkeys(evidence.config_paths))),
        metric_line("Effective config names in log", ", ".join(dict.fromkeys(evidence.config_names))),
        metric_line("Effective MAVLink rates in log", latest_rates or "not captured"),
        metric_line("HIL_SENSOR send-rate mean from log", f"{sensor_rate_mean:.2f} Hz" if sensor_rate_mean else None),
        metric_line("Estimated callback/FPS mean from log", f"{fps_mean:.2f} Hz" if fps_mean else None),
        metric_line("Estimated callback/FPS min from log", f"{fps_min:.2f} Hz" if fps_min else None),
        metric_line("TimestampProvider lines", len(evidence.timestamp_lines)),
        metric_line("Transport events", len(evidence.transport_events)),
        metric_line("Transport/drop alerts", len(evidence.transport_alerts)),
        "",
        "## PX4 Offset Capture Evidence",
        metric_line("Legacy undated PX4 output", px4_output or "not provided"),
        metric_line(
            "Offset captures",
            ", ".join(f"{capture.label}@{capture.wall_time_utc}" for capture in px4_captures),
        ),
        metric_line(
            "Capture files",
            ", ".join(capture.output_path.name for capture in px4_captures),
        ),
        "- Validator/failsafe `YES` lines without `t0`/`t5`/`t30` offset labels are historical/non-decisive.",
        "- Use `px4_capture_diff.txt` to separate transient startup failures from persistent failures.",
        "- Use `px4_command_sheet.md` for the full read-only command set.",
        "",
        "## EKF2 Gate Readout Order",
        "| Gate | Signal | Capture evidence |",
        "|---|---|---|",
        *(f"| {gate} | {signal} | {evidence} |" for gate, signal, evidence in PX4_GATE_READOUT),
        "",
        "## Decision Rules",
        "- render FPS approximately callback Hz approximately HIL_SENSOR send Hz approximately PX4 IMU rate approximately 24: frame/callback-bound operation; do not assume scheduler fix first.",
        "- callback Hz greater than HIL_SENSOR send Hz: scheduler gating/throttle issue.",
        "- HIL_SENSOR send Hz greater than PX4 received Hz: transport/drop/backpressure issue.",
        "- time_usec deltas distorted or drift from callback/wall time: timestamp-clock fix must precede cadence changes.",
        "- installed config differs from repo config: fix packaging/install/config drift and rerun diagnostics.",
        "- PX4 lockstep confirmed: catch-up scheduling is forbidden unless separately proven lockstep-safe.",
        "- PX4 requires >=50 Hz and target machine cannot sustain >=50 callback Hz: catch-up/async publishing may only be considered after a shadow-mode bias/timestamp test.",
        "",
        "## Human HITL Checklist",
        "- Installed plugin path:",
        "- Installed config.ini hash/diff vs repo:",
        "- X-Plane render FPS mean/min:",
        "- X-Plane paused, backgrounded, in menu, FPS-limited, or graphics-limited:",
        "- PX4 effective IMU_INTEG_RATE after clamp:",
        "- PX4 observed vehicle_imu rate:",
        "- PX4 observed vehicle_acceleration rate:",
        "- PX4 observed vehicle_angular_velocity rate:",
        "- Accel validator status:",
        "- Gyro validator status:",
        "- EKF2 update event count:",
        "- EKF2 time_slip:",
        "- Timestamp/bias/innovation warnings verbatim:",
        "- PX4 lockstep-sensitive? yes/no/unknown with evidence:",
        "",
        "## PX4 Commands To Run And Paste",
        "```text",
        *PX4_CAPTURE_COMMANDS,
        "```",
        "",
        "## Files",
        "- xplane_px4xplane_excerpts.log",
        "- transport_events.jsonl",
        "- timestamp_lines.log",
        "- transport_alerts.log",
        "- installed_config.ini",
        "- installed_vs_repo_config.diff",
        "- px4_command_sheet.md",
        "- px4_t0.txt, px4_t5.txt, px4_t30.txt when provided",
        "- px4_capture_diff.txt",
        "- px4_output.txt, when provided",
    ]
    return "\n".join(lines) + "\n"


def write_bundle(
    output_dir: Path,
    xplane_log: Path,
    installed_config: Path,
    px4_output: Path | None,
    px4_output_t0: Path | None = None,
    px4_output_t5: Path | None = None,
    px4_output_t30: Path | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence = parse_log(xplane_log)
    capture_inputs = {
        "t0": px4_output_t0,
        "t5": px4_output_t5,
        "t30": px4_output_t30,
    }

    (output_dir / "xplane_px4xplane_excerpts.log").write_text(
        "\n".join(evidence.excerpts) + ("\n" if evidence.excerpts else ""),
        encoding="utf-8",
    )
    (output_dir / "transport_events.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in evidence.transport_events),
        encoding="utf-8",
    )
    (output_dir / "timestamp_lines.log").write_text(
        "\n".join(evidence.timestamp_lines) + ("\n" if evidence.timestamp_lines else ""),
        encoding="utf-8",
    )
    (output_dir / "transport_alerts.log").write_text(
        "\n".join(evidence.transport_alerts) + ("\n" if evidence.transport_alerts else ""),
        encoding="utf-8",
    )

    if installed_config.is_file():
        shutil.copy2(installed_config, output_dir / "installed_config.ini")
    if xplane_log.is_file():
        shutil.copy2(xplane_log, output_dir / "Log.txt")
    if px4_output and px4_output.is_file():
        shutil.copy2(px4_output, output_dir / "px4_output.txt")

    px4_captures = write_px4_captures(output_dir, capture_inputs)
    (output_dir / "px4_capture_diff.txt").write_text(render_px4_capture_diff(px4_captures), encoding="utf-8")
    (output_dir / "px4_command_sheet.md").write_text(render_px4_command_sheet(), encoding="utf-8")

    config_diff = unified_config_diff(installed_config)
    (output_dir / "installed_vs_repo_config.diff").write_text(config_diff, encoding="utf-8")

    summary = render_summary(
        output_dir=output_dir,
        xplane_log=xplane_log,
        installed_config=installed_config,
        px4_output=px4_output,
        px4_captures=px4_captures,
        evidence=evidence,
        installed_hash=sha256_file(installed_config),
        repo_hash=sha256_file(REPO_CONFIG),
        config_diff=config_diff,
    )
    (output_dir / "README.md").write_text(summary, encoding="utf-8")
    return output_dir


def run_self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="px4xplane-hitl-diag-") as tmp:
        tmp_path = Path(tmp)
        sample_log = tmp_path / "Log.txt"
        sample_config = tmp_path / "config.ini"
        sample_px4 = tmp_path / "px4.txt"
        sample_px4_t0 = tmp_path / "px4_t0.txt"
        sample_px4_t5 = tmp_path / "px4_t5.txt"
        sample_px4_t30 = tmp_path / "px4_t30.txt"
        out_dir = tmp_path / "bundle"

        sample_log.write_text(
            "\n".join(
                [
                    "px4xplane: Resolved config file path: /tmp/config.ini",
                    "px4xplane: Loading config file from: /tmp/config.ini",
                    "px4xplane: MAVLink rates - SENSOR:200Hz GPS:10Hz STATE:50Hz RC:50Hz",
                    "px4xplane: Message periods initialized - SENSOR:0.0050s (200Hz) GPS:0.1000s (10Hz) STATE:0.0200s (50Hz) RC:0.0200s (50Hz)",
                    "px4xplane: Config Name: Alia250",
                    "px4xplane: [20.0s] HIL_SENSOR: 1000 msgs, avg 24.3 Hz (target 200 Hz)",
                    "px4xplane: [RATE] target Hz SENSOR:60 GPS:5 STATE:20 RC:20; achieved Hz SENSOR:26.5 GPS:4.8 STATE:13.9 RC:13.9; X-Plane:52.3 FPS; policy=clamp+warn/no-catch-up/no-auto-degrade",
                    'px4xplane: [TRANSPORT_EVENT] {"event":"client_connected","estimated_fps":24.6,"flight_loop_counter":42}',
                    "px4xplane: [TIMESTAMP] t=20.0s, drift=+1.000ms, avg_delta=40.65ms",
                    "px4xplane: Send backpressure from PX4 client; dropping this frame (count=1).",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        sample_config.write_text(REPO_CONFIG.read_text(encoding="utf-8-sig", errors="replace"), encoding="utf-8")
        sample_px4.write_text("param show IMU_INTEG_RATE\n", encoding="utf-8")
        sample_px4_t0.write_text("commander check\nAccel #0 fail: YES\n", encoding="utf-8")
        sample_px4_t5.write_text("commander check\nAccel #0 fail: YES\n", encoding="utf-8")
        sample_px4_t30.write_text("commander check\nAccel #0 fail: NO\n", encoding="utf-8")

        write_bundle(out_dir, sample_log, sample_config, sample_px4, sample_px4_t0, sample_px4_t5, sample_px4_t30)
        summary = (out_dir / "README.md").read_text(encoding="utf-8")
        required = [
            "HIL_SENSOR send-rate mean from log: 25.40 Hz",
            "Estimated callback/FPS mean from log: 38.45 Hz",
            "Offset captures: t0@",
            "px4_command_sheet.md",
            "uorb top -1 sensor_baro",
            "PX4 Commands To Run And Paste",
            "frame/callback-bound operation",
        ]
        missing = [text for text in required if text not in summary]
        expected_files = [
            out_dir / "px4_t0.txt",
            out_dir / "px4_t5.txt",
            out_dir / "px4_t30.txt",
            out_dir / "px4_capture_diff.txt",
            out_dir / "px4_command_sheet.md",
        ]
        missing.extend(str(path.name) for path in expected_files if not path.is_file())
        diff_text = (out_dir / "px4_capture_diff.txt").read_text(encoding="utf-8")
        if "Diff t0 -> t5" not in diff_text or "Diff t5 -> t30" not in diff_text:
            missing.append("px4_capture_diff adjacent offsets")
        if "# px4_capture_offset: t30" not in (out_dir / "px4_t30.txt").read_text(encoding="utf-8"):
            missing.append("px4_t30 offset header")
        if missing:
            print(f"[FAIL] hitl-diagnostic-bundle-self-test missing: {missing}")
            return 1

    print("[PASS] hitl-diagnostic-bundle-self-test")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xplane-log", type=Path, default=DEFAULT_XPLANE_LOG, help="Path to X-Plane Log.txt.")
    parser.add_argument(
        "--installed-config",
        type=Path,
        default=DEFAULT_INSTALLED_CONFIG,
        help="Path to installed px4xplane/64/config.ini.",
    )
    parser.add_argument("--px4-output", type=Path, help="Optional text file containing copied PX4 shell output.")
    parser.add_argument("--px4-output-t0", type=Path, help="PX4 shell output captured immediately after connect.")
    parser.add_argument("--px4-output-t5", type=Path, help="PX4 shell output captured five seconds after t0.")
    parser.add_argument("--px4-output-t30", type=Path, help="PX4 shell output captured thirty seconds after t0.")
    parser.add_argument("--output-dir", type=Path, help="Directory for the evidence bundle.")
    parser.add_argument("--self-test", action="store_true", help="Run the parser/bundle self-test.")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = args.output_dir or DEFAULT_OUTPUT_ROOT / f"hitl-cadence-{stamp}"
    bundle_dir = write_bundle(
        output_dir,
        args.xplane_log,
        args.installed_config,
        args.px4_output,
        args.px4_output_t0,
        args.px4_output_t5,
        args.px4_output_t30,
    )
    print(f"Created HITL cadence evidence bundle: {bundle_dir}")
    if not args.xplane_log.is_file():
        print(f"[WARN] X-Plane log not found: {args.xplane_log}")
    if not args.installed_config.is_file():
        print(f"[WARN] installed config not found: {args.installed_config}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
