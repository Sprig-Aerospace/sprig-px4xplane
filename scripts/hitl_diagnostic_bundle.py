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

# The only diag_version this parser understands. Any line carrying a different
# diag_version is recorded to LogEvidence.version_mismatch and skipped (fail
# closed) rather than being silently parsed as v1. Bump this in lockstep with
# the C++ emitters and the golden fixtures under scripts/testdata/.
SUPPORTED_DIAG_VERSION = 1
GOLDEN_LINES_FILE = ROOT / "scripts" / "testdata" / "diag_golden_lines.txt"
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
RATE_RE = re.compile(
    r"\[RATE\]\s+diag_version=(?P<diag_version>\d+)\s+generation=(?P<generation>\d+)\s+"
    r"wall_time_usec=(?P<wall_time_usec>\d+).*?\s+rate_hz=(?P<sensor>[0-9.]+|unmeasured)\s+"
    r"target_hz=(?P<target>\d+)\s+estimated_fps=(?P<xplane_fps>[0-9.]+)"
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
TIMESTAMP_RE = re.compile(
    r"\[TIMESTAMP\]\s+diag_version=(?P<diag_version>\d+)\s+"
    r"generation=(?P<generation>\d+)\s+(?P<message>.+)"
)
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
    version_mismatch: list[str] = field(default_factory=list)


@dataclass
class SessionBoundary:
    status: str = "log-missing"
    transport_generation: int | None = None
    client_connected_line: int | None = None
    session_reset_line: int | None = None
    current_start_line: int | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class ParsedLog:
    current: LogEvidence = field(default_factory=LogEvidence)
    historical: LogEvidence = field(default_factory=LogEvidence)
    boundary: SessionBoundary = field(default_factory=SessionBoundary)


@dataclass
class TransportEventRecord:
    line_number: int
    event: dict[str, object]


@dataclass
class PX4Capture:
    label: str
    offset_sec: int
    source_path: Path
    output_path: Path
    scheduled_utc: str
    started_utc: str
    finished_utc: str
    command_set: str


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


def parse_transport_event(line: str) -> dict[str, object] | None:
    if match := TRANSPORT_EVENT_RE.search(line):
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def event_transport_generation(event: dict[str, object]) -> int | None:
    # #19 renamed the transport-session reset counter to transport_generation
    # (distinct from the cross-grammar RATE/TIMESTAMP `generation`). Session
    # partitioning is driven by this transport-session counter.
    generation = event.get("transport_generation")
    if isinstance(generation, bool):
        return None
    if isinstance(generation, int):
        return generation
    return None


def determine_session_boundary(lines: list[str], transport_events: list[TransportEventRecord]) -> SessionBoundary:
    if not lines:
        return SessionBoundary(status="empty-log", notes=["Log is empty or unavailable."])

    # Fail closed: only trust transport events of the supported diag_version for
    # boundary detection, mirroring parse_log's version lock.
    supported_events = [
        record
        for record in transport_events
        if record.event.get("diag_version") == SUPPORTED_DIAG_VERSION
    ]

    generations = [event_transport_generation(record.event) for record in supported_events]
    numeric_generations = [generation for generation in generations if generation is not None]
    if not numeric_generations:
        return SessionBoundary(
            status="no-session-boundary",
            current_start_line=1,
            notes=["No transport_generation was found; all parsed log lines are treated as current compatibility evidence."],
        )

    highest_generation = max(numeric_generations)
    records = [
        record
        for record in supported_events
        if event_transport_generation(record.event) == highest_generation
    ]
    client_records = [
        record
        for record in records
        if record.event.get("event") == "client_connected"
    ]
    reset_records = [
        record
        for record in records
        if record.event.get("event") == "session_reset"
    ]

    latest_client = client_records[-1] if client_records else None
    if latest_client:
        reset_after_client = [
            record
            for record in reset_records
            if record.line_number >= latest_client.line_number
        ]
    else:
        reset_after_client = reset_records

    if reset_after_client:
        reset = reset_after_client[-1]
        return SessionBoundary(
            status="complete-session",
            transport_generation=highest_generation,
            client_connected_line=latest_client.line_number if latest_client else None,
            session_reset_line=reset.line_number,
            current_start_line=reset.line_number,
            notes=["Current readiness includes only lines at or after the latest session_reset boundary."],
        )

    if latest_client:
        return SessionBoundary(
            status="incomplete-session",
            transport_generation=highest_generation,
            client_connected_line=latest_client.line_number,
            current_start_line=latest_client.line_number,
            notes=["Latest transport_generation has client_connected but no following session_reset; using client_connected fallback."],
        )

    return SessionBoundary(
        status="no-current-session",
        transport_generation=highest_generation,
        current_start_line=len(lines) + 1,
        notes=["Transport generations exist, but the highest transport_generation has no client_connected/session_reset boundary."],
    )


def append_line_evidence(evidence: LogEvidence, line: str) -> None:
    if any(pattern in line for pattern in PX4XPLANE_PATTERNS):
        evidence.excerpts.append(line)

    if match := TRANSPORT_EVENT_RE.search(line):
        try:
            event = json.loads(match.group(1))
        except json.JSONDecodeError:
            evidence.transport_alerts.append(line)
        else:
            # Fail closed: only parse transport events of the supported
            # diag_version. Unknown versions are recorded, not trusted.
            if event.get("diag_version") != SUPPORTED_DIAG_VERSION:
                evidence.version_mismatch.append(line)
            else:
                evidence.transport_events.append(event)
                fps = event.get("estimated_fps")
                if isinstance(fps, (int, float)) and fps > 0:
                    evidence.estimated_fps.append(float(fps))

    if match := RATE_RE.search(line):
        if int(match.group("diag_version")) != SUPPORTED_DIAG_VERSION:
            evidence.version_mismatch.append(line)
        else:
            # rate_hz=unmeasured marks an empty wall window (honest-metrics);
            # skip it from numeric rate aggregation rather than crashing.
            sensor_rate = match.group("sensor")
            if sensor_rate != "unmeasured":
                evidence.hil_sensor_rates_hz.append(float(sensor_rate))
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
        if int(match.group("diag_version")) != SUPPORTED_DIAG_VERSION:
            evidence.version_mismatch.append(line)
        else:
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


def is_stale_client_replaced(line: str) -> bool:
    """Return True for a supported-version stale_client_replaced transport event.

    A stale_client_replaced records the teardown of a previous transport session
    and is pre-boundary by definition. It must NEVER be counted as current-session
    evidence, even if hostile/malformed log ordering places it after the
    session_reset / client_connected boundary line.

    Fail closed on diag_version: an unknown-version stale event does not match
    here, so it falls through to the line-number partition where
    append_line_evidence records it as a version_mismatch (not trusted).
    """
    event = parse_transport_event(line)
    return (
        event is not None
        and event.get("event") == "stale_client_replaced"
        and event.get("diag_version") == SUPPORTED_DIAG_VERSION
    )


def parse_log(path: Path) -> ParsedLog:
    parsed = ParsedLog()
    if not path.is_file():
        return parsed

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        lines = [raw_line.rstrip("\n") for raw_line in handle]

    transport_events = [
        TransportEventRecord(line_number=index, event=event)
        for index, line in enumerate(lines, start=1)
        if (event := parse_transport_event(line)) is not None
    ]
    parsed.boundary = determine_session_boundary(lines, transport_events)
    current_start_line = parsed.boundary.current_start_line or 1

    for index, line in enumerate(lines, start=1):
        if is_stale_client_replaced(line):
            # Always historical: a stale replacement is pre-boundary by
            # definition, regardless of its line position. This overrides the
            # line-number partition so a hostile log cannot smuggle a stale
            # event into the current partition by placing it after the boundary.
            append_line_evidence(parsed.historical, line)
        elif index < current_start_line:
            append_line_evidence(parsed.historical, line)
        else:
            append_line_evidence(parsed.current, line)

    return parsed


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def metric_line(label: str, value: object) -> str:
    return f"- {label}: {value if value not in (None, '') else 'not captured'}"


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
        "Each capture file must include in-band UTC metadata headers. File modification time is not authoritative.",
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
            "  --px4-capture-dir /path/to/px4-timed-diagnostics-latest",
            "",
            "# Or pass files explicitly:",
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


def parse_px4_capture_headers(text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("# px4_capture_"):
            continue
        key, _, value = line[2:].partition(":")
        if key and value:
            headers[key.strip()] = value.strip()
    return headers


def iso_like_utc(value: str) -> bool:
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$", value):
        return True
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00$", value):
        return True
    if value.startswith("unix_ms:") and value.removeprefix("unix_ms:").isdigit():
        return True
    return False


def px4_capture_inputs_from_dir(capture_dir: Path | None) -> dict[str, Path | None]:
    if not capture_dir:
        return {}
    return {label: capture_dir / f"px4_{label}.txt" for label, _ in PX4_CAPTURE_OFFSETS}


def load_px4_capture_manifest(capture_dir: Path | None) -> dict[str, object]:
    if not capture_dir:
        return {}
    manifest_path = capture_dir / "timed_capture_manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"manifest_error": f"invalid JSON in {manifest_path}"}
    return parsed if isinstance(parsed, dict) else {"manifest_error": f"unexpected manifest shape in {manifest_path}"}


def validate_capture_metadata(label: str, offset_sec: int, source_path: Path, text: str) -> dict[str, str]:
    headers = parse_px4_capture_headers(text)
    required = {
        "px4_capture_offset": label,
        "px4_capture_offset_sec": str(offset_sec),
        "px4_capture_scheduled_utc": None,
        "px4_capture_started_utc": None,
        "px4_capture_finished_utc": None,
    }
    missing = [key for key in required if not headers.get(key)]
    if missing:
        raise ValueError(f"{source_path} missing required in-band capture metadata: {', '.join(missing)}")

    for key, expected in required.items():
        if expected is not None and headers.get(key) != expected:
            raise ValueError(f"{source_path} has {key}={headers.get(key)!r}, expected {expected!r}")

    for key in (
        "px4_capture_scheduled_utc",
        "px4_capture_started_utc",
        "px4_capture_finished_utc",
    ):
        value = headers[key]
        if not iso_like_utc(value):
            raise ValueError(f"{source_path} has non-UTC-looking {key}: {value!r}")

    return headers


def assert_capture_monotonicity(captures: list[PX4Capture]) -> None:
    if len(captures) < 2:
        return
    ordered_labels = [label for label, _ in PX4_CAPTURE_OFFSETS]
    captures_by_label = {capture.label: capture for capture in captures}
    ordered = [captures_by_label[label] for label in ordered_labels if label in captures_by_label]
    for previous, current in zip(ordered, ordered[1:]):
        if previous.scheduled_utc >= current.scheduled_utc:
            raise ValueError(
                "PX4 capture timestamps are not monotonic: "
                f"{previous.label}@{previous.scheduled_utc} !< {current.label}@{current.scheduled_utc}"
            )


def write_px4_captures(output_dir: Path, capture_inputs: dict[str, Path | None]) -> list[PX4Capture]:
    captures: list[PX4Capture] = []
    for label, offset_sec in PX4_CAPTURE_OFFSETS:
        source_path = capture_inputs.get(label)
        if not source_path or not source_path.is_file():
            continue

        source_text = read_text_if_exists(source_path)
        headers = validate_capture_metadata(label, offset_sec, source_path, source_text)
        output_path = output_dir / f"px4_{label}.txt"
        output_path.write_text(source_text.rstrip("\n") + "\n", encoding="utf-8")
        captures.append(
            PX4Capture(
                label=label,
                offset_sec=offset_sec,
                source_path=source_path,
                output_path=output_path,
                scheduled_utc=headers["px4_capture_scheduled_utc"],
                started_utc=headers["px4_capture_started_utc"],
                finished_utc=headers["px4_capture_finished_utc"],
                command_set=headers.get("px4_capture_command_set", "unknown"),
            )
        )
    assert_capture_monotonicity(captures)
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
    historical_evidence: LogEvidence,
    boundary: SessionBoundary,
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
    historical_count = (
        len(historical_evidence.excerpts)
        + len(historical_evidence.transport_events)
        + len(historical_evidence.timestamp_lines)
        + len(historical_evidence.transport_alerts)
    )

    lines = [
        "# px4xplane HITL Cadence Evidence",
        "",
        "This bundle is an evidence gate before scheduler changes. Current-readiness metrics are session-partitioned and do not prove the HITL issue is fixed.",
        "",
        "## Inputs",
        metric_line("Bundle directory", output_dir),
        metric_line("X-Plane log", xplane_log),
        metric_line("Installed config", installed_config),
        metric_line("PX4 output", px4_output or "not provided"),
        "",
        "## Session Boundary",
        metric_line("Boundary status", boundary.status),
        metric_line("Current transport_generation", boundary.transport_generation),
        metric_line("client_connected line", boundary.client_connected_line),
        metric_line("session_reset line", boundary.session_reset_line),
        metric_line("Current evidence starts at line", boundary.current_start_line),
        metric_line("Historical evidence retained", "yes" if historical_count else "no"),
        metric_line("Boundary notes", "; ".join(boundary.notes)),
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
        "## Current Session Log Evidence",
        metric_line("Resolved config paths in log", ", ".join(dict.fromkeys(evidence.config_paths))),
        metric_line("Effective config names in log", ", ".join(dict.fromkeys(evidence.config_names))),
        metric_line("Effective MAVLink rates in log", latest_rates or "not captured"),
        metric_line("HIL_SENSOR send-rate mean from log", f"{sensor_rate_mean:.2f} Hz" if sensor_rate_mean else None),
        metric_line("Estimated callback/FPS mean from log", f"{fps_mean:.2f} Hz" if fps_mean else None),
        metric_line("Estimated callback/FPS min from log", f"{fps_min:.2f} Hz" if fps_min else None),
        metric_line("TimestampProvider lines", len(evidence.timestamp_lines)),
        metric_line("Transport events", len(evidence.transport_events)),
        metric_line("Transport/drop alerts", len(evidence.transport_alerts)),
        metric_line(
            f"Diag version mismatches (expected diag_version={SUPPORTED_DIAG_VERSION}, skipped)",
            len(evidence.version_mismatch),
        ),
        "",
        "## Historical Evidence",
        "Pre-boundary lines are retained for forensics only and are excluded from current-readiness aggregates.",
        metric_line("Historical excerpts", len(historical_evidence.excerpts)),
        metric_line("Historical transport events", len(historical_evidence.transport_events)),
        metric_line("Historical TimestampProvider lines", len(historical_evidence.timestamp_lines)),
        metric_line("Historical transport/drop alerts", len(historical_evidence.transport_alerts)),
        metric_line("Historical diag version mismatches", len(historical_evidence.version_mismatch)),
        "",
        "## PX4 Offset Capture Evidence",
        metric_line("Legacy undated PX4 output", px4_output or "not provided"),
        metric_line(
            "Offset captures",
            ", ".join(
                f"{capture.label}@scheduled:{capture.scheduled_utc}/started:{capture.started_utc}/finished:{capture.finished_utc}"
                for capture in px4_captures
            ),
        ),
        metric_line(
            "Capture command sets",
            ", ".join(f"{capture.label}:{capture.command_set}" for capture in px4_captures),
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
        "- version_mismatch.log",
        "- session_boundary.json",
        "- historical/xplane_px4xplane_excerpts.log",
        "- historical/transport_events.jsonl",
        "- historical/timestamp_lines.log",
        "- historical/transport_alerts.log",
        "- historical/version_mismatch.log",
        "- installed_config.ini",
        "- installed_vs_repo_config.diff",
        "- px4_command_sheet.md",
        "- timed_capture_manifest.json, when provided by the launcher",
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
    px4_capture_dir: Path | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    parsed_log = parse_log(xplane_log)
    evidence = parsed_log.current
    historical_evidence = parsed_log.historical
    historical_dir = output_dir / "historical"
    historical_dir.mkdir(parents=True, exist_ok=True)
    capture_inputs = px4_capture_inputs_from_dir(px4_capture_dir)
    capture_inputs.update(
        {
            "t0": px4_output_t0 or capture_inputs.get("t0"),
            "t5": px4_output_t5 or capture_inputs.get("t5"),
            "t30": px4_output_t30 or capture_inputs.get("t30"),
        }
    )
    capture_manifest = load_px4_capture_manifest(px4_capture_dir)

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
    (output_dir / "version_mismatch.log").write_text(
        "\n".join(evidence.version_mismatch) + ("\n" if evidence.version_mismatch else ""),
        encoding="utf-8",
    )
    (output_dir / "session_boundary.json").write_text(
        json.dumps(
            {
                "status": parsed_log.boundary.status,
                "transport_generation": parsed_log.boundary.transport_generation,
                "client_connected_line": parsed_log.boundary.client_connected_line,
                "session_reset_line": parsed_log.boundary.session_reset_line,
                "current_start_line": parsed_log.boundary.current_start_line,
                "notes": parsed_log.boundary.notes,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (historical_dir / "xplane_px4xplane_excerpts.log").write_text(
        "\n".join(historical_evidence.excerpts) + ("\n" if historical_evidence.excerpts else ""),
        encoding="utf-8",
    )
    (historical_dir / "transport_events.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in historical_evidence.transport_events),
        encoding="utf-8",
    )
    (historical_dir / "timestamp_lines.log").write_text(
        "\n".join(historical_evidence.timestamp_lines) + ("\n" if historical_evidence.timestamp_lines else ""),
        encoding="utf-8",
    )
    (historical_dir / "transport_alerts.log").write_text(
        "\n".join(historical_evidence.transport_alerts) + ("\n" if historical_evidence.transport_alerts else ""),
        encoding="utf-8",
    )
    (historical_dir / "version_mismatch.log").write_text(
        "\n".join(historical_evidence.version_mismatch) + ("\n" if historical_evidence.version_mismatch else ""),
        encoding="utf-8",
    )

    if installed_config.is_file():
        shutil.copy2(installed_config, output_dir / "installed_config.ini")
    if xplane_log.is_file():
        shutil.copy2(xplane_log, output_dir / "Log.txt")
    if px4_output and px4_output.is_file():
        shutil.copy2(px4_output, output_dir / "px4_output.txt")
    if px4_capture_dir and (px4_capture_dir / "timed_capture_manifest.json").is_file():
        shutil.copy2(px4_capture_dir / "timed_capture_manifest.json", output_dir / "timed_capture_manifest.json")
    if capture_manifest:
        (output_dir / "timed_capture_manifest.normalized.json").write_text(
            json.dumps(capture_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

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
        historical_evidence=historical_evidence,
        boundary=parsed_log.boundary,
        installed_hash=sha256_file(installed_config),
        repo_hash=sha256_file(REPO_CONFIG),
        config_diff=config_diff,
    )
    (output_dir / "README.md").write_text(summary, encoding="utf-8")
    return output_dir


def load_golden_lines() -> dict[str, list[str]]:
    """Load emitter-derived golden log lines grouped by KIND.

    The fixture (scripts/testdata/diag_golden_lines.txt) reproduces the exact
    field list/order/format tokens of the C++ snprintf() emitters. Self-test
    asserts the parser regexes match these goldens so C++ format drift is caught
    in review. Regenerate the fixture whenever the C++ format strings change.
    """
    grouped: dict[str, list[str]] = {}
    text = GOLDEN_LINES_FILE.read_text(encoding="utf-8")
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        kind, _, golden = raw.partition("\t")
        grouped.setdefault(kind.strip(), []).append(golden)
    return grouped


def run_self_test() -> int:
    # --- Part 1: emitter-derived golden parity ---------------------------------
    # Assert the parser regexes match the golden lines that mirror the C++
    # snprintf() format strings, and that diag_version/generation parse to the
    # expected values. Expected per fixture annotation: diag_version=1 and
    # generation=2 on RATE/TIMESTAMP/SUMMARY lines; the TRANSPORT golden carries
    # transport_generation (transport-session counter) = 1, not the cross-grammar
    # generation. RATE goldens include a rate_hz=unmeasured (empty-window) variant.
    goldens = load_golden_lines()
    for kind in ("RATE", "TIMESTAMP", "SUMMARY", "TRANSPORT"):
        if not goldens.get(kind):
            print(f"[FAIL] golden fixture missing KIND={kind}")
            return 1

    saw_unmeasured_rate = False
    for line in goldens["RATE"]:
        m = RATE_RE.search(line)
        if not m:
            print(f"[FAIL] RATE_RE did not match golden: {line}")
            return 1
        if int(m.group("diag_version")) != 1 or int(m.group("generation")) != 2:
            print(f"[FAIL] RATE golden version/generation mismatch: {line}")
            return 1
        # honest-metrics: rate_hz=unmeasured marks an empty wall window. The regex
        # must capture it as the literal token (not a float) so aggregation skips it.
        if m.group("sensor") == "unmeasured":
            saw_unmeasured_rate = True
    if not saw_unmeasured_rate:
        print("[FAIL] RATE goldens missing rate_hz=unmeasured variant")
        return 1

    for line in goldens["TIMESTAMP"]:
        m = TIMESTAMP_RE.search(line)
        if not m:
            print(f"[FAIL] TIMESTAMP_RE did not match golden: {line}")
            return 1
        if int(m.group("diag_version")) != 1 or int(m.group("generation")) != 2:
            print(f"[FAIL] TIMESTAMP golden version/generation mismatch: {line}")
            return 1

    for line in goldens["TRANSPORT"]:
        tm = TRANSPORT_EVENT_RE.search(line)
        if not tm:
            print(f"[FAIL] TRANSPORT_EVENT_RE did not match golden: {line}")
            return 1
        event = json.loads(tm.group(1))
        # transport_generation is the transport-session reset counter (distinct
        # from the cross-grammar `generation`). First-session client_connected
        # emits transport_generation=1 (ConnectionManager.cpp:531 increments
        # 0->1 just before the emit at :543).
        if event.get("diag_version") != 1 or event.get("transport_generation") != 1:
            print(f"[FAIL] TRANSPORT golden version/transport_generation mismatch: {line}")
            return 1

    # --- Part 2: session partitioning over the versioned grammar ---------------
    # A later transport session (transport_generation=2) with a session_reset
    # establishes the current-session boundary. Earlier gen-1 evidence and a
    # gen-2 stale_client_replaced (pre-boundary) land in the historical
    # partition. A v2 line on each grammar must fail closed (version_mismatch).
    with tempfile.TemporaryDirectory(prefix="px4xplane-hitl-diag-") as tmp:
        tmp_path = Path(tmp)
        sample_log = tmp_path / "Log.txt"
        sample_config = tmp_path / "config.ini"
        sample_px4 = tmp_path / "px4.txt"
        sample_px4_t0 = tmp_path / "px4_t0.txt"
        sample_px4_t5 = tmp_path / "px4_t5.txt"
        sample_px4_t30 = tmp_path / "px4_t30.txt"
        out_dir = tmp_path / "bundle"

        v1_rate = goldens["RATE"][0]
        unmeasured_rate = next(
            (r for r in goldens["RATE"] if "rate_hz=unmeasured" in r), None
        )
        if unmeasured_rate is None:
            print("[FAIL] no rate_hz=unmeasured golden available for parse test")
            return 1
        v1_timestamp = goldens["TIMESTAMP"][0]
        v1_transport = goldens["TRANSPORT"][0]
        v2_rate = v1_rate.replace("diag_version=1", "diag_version=2", 1)
        v2_timestamp = v1_timestamp.replace("diag_version=1", "diag_version=2", 1)
        v2_transport = v1_transport.replace('"diag_version":1', '"diag_version":2', 1)

        # Current transport session (transport_generation=2): a stale replacement
        # then a client_connected and a session_reset boundary.
        stale_replaced = (
            'px4xplane: [TRANSPORT_EVENT] '
            '{"diag_version":1,"wall_time_usec":123457050,"event":"stale_client_replaced",'
            '"transport_generation":2,"estimated_fps":40.0,"flight_loop_counter":79}'
        )
        current_client = (
            'px4xplane: [TRANSPORT_EVENT] '
            '{"diag_version":1,"wall_time_usec":123457000,"event":"client_connected",'
            '"transport_generation":2,"estimated_fps":52.0,"flight_loop_counter":80}'
        )
        current_reset = (
            'px4xplane: [TRANSPORT_EVENT] '
            '{"diag_version":1,"wall_time_usec":123457100,"event":"session_reset",'
            '"transport_generation":2,"estimated_fps":52.0,"flight_loop_counter":81}'
        )
        # A current-session RATE line (after the boundary) confirms current
        # aggregation reads #19's versioned grammar.
        current_rate = (
            "px4xplane: [RATE] diag_version=1 generation=2 wall_time_usec=123458000 "
            "sim_time=42.0s HIL_SENSOR window_msgs=1000 total_msgs=2000 wall_window_sec=40.000 "
            "rate_hz=28.00 target_hz=60 estimated_fps=53.0 paused=0 "
            "dt_p50_bucket_usec=45000 dt_p95_bucket_usec=60000 dt_max_usec=61000"
        )

        sample_log.write_text(
            "\n".join(
                [
                    "px4xplane: Resolved config file path: /tmp/config.ini",
                    "px4xplane: Loading config file from: /tmp/config.ini",
                    "px4xplane: MAVLink rates - SENSOR:200Hz GPS:10Hz STATE:50Hz RC:50Hz",
                    "px4xplane: Message periods initialized - SENSOR:0.0050s (200Hz) GPS:0.1000s (10Hz) STATE:0.0200s (50Hz) RC:0.0200s (50Hz)",
                    "px4xplane: Config Name: Alia250",
                    v1_rate,
                    unmeasured_rate,
                    v1_transport,
                    v1_timestamp,
                    v2_rate,
                    v2_transport,
                    v2_timestamp,
                    "px4xplane: Send backpressure from PX4 client; dropping this frame (count=1).",
                    stale_replaced,
                    current_client,
                    current_reset,
                    current_rate,
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        sample_config.write_text(REPO_CONFIG.read_text(encoding="utf-8-sig", errors="replace"), encoding="utf-8")
        sample_px4.write_text("param show IMU_INTEG_RATE\n", encoding="utf-8")
        for label, offset, scheduled, started, finished, body in [
            ("t0", 0, "2026-06-15T22:15:03Z", "2026-06-15T22:15:03Z", "2026-06-15T22:15:18Z", "commander check\nAccel #0 fail: YES\n"),
            ("t5", 5, "2026-06-15T22:15:08Z", "2026-06-15T22:15:08Z", "2026-06-15T22:15:23Z", "commander check\nAccel #0 fail: YES\n"),
            ("t30", 30, "2026-06-15T22:15:33Z", "2026-06-15T22:15:33Z", "2026-06-15T22:15:48Z", "commander check\nAccel #0 fail: NO\n"),
        ]:
            (tmp_path / f"px4_{label}.txt").write_text(
                "\n".join(
                    [
                        f"# px4_capture_offset: {label}",
                        f"# px4_capture_offset_sec: {offset}",
                        f"# px4_capture_scheduled_utc: {scheduled}",
                        f"# px4_capture_started_utc: {started}",
                        f"# px4_capture_finished_utc: {finished}",
                        "# px4_capture_command_set: launcher-timed-px4-diagnostics-v1",
                        "",
                        body.rstrip("\n"),
                        "",
                    ]
                ),
                encoding="utf-8",
            )

        parsed = parse_log(sample_log)
        evidence = parsed.current
        historical = parsed.historical

        if parsed.boundary.status != "complete-session":
            print(f"[FAIL] expected complete-session boundary, got {parsed.boundary.status}")
            return 1
        if parsed.boundary.transport_generation != 2:
            print(f"[FAIL] expected current transport_generation=2, got {parsed.boundary.transport_generation}")
            return 1

        # Current partition: only the gen-2 current_rate (28.0) contributes to
        # numeric aggregation; the gen-1 goldens are historical.
        if evidence.hil_sensor_rates_hz != [28.0]:
            print(f"[FAIL] current RATE aggregation wrong: {evidence.hil_sensor_rates_hz}")
            return 1
        if 25.0 in evidence.hil_sensor_rates_hz:
            print(f"[FAIL] historical gen-1 rate leaked into current: {evidence.hil_sensor_rates_hz}")
            return 1
        if 25.0 not in historical.hil_sensor_rates_hz:
            print(f"[FAIL] historical gen-1 rate missing from historical: {historical.hil_sensor_rates_hz}")
            return 1
        # Current partition begins at the session_reset boundary line, so the
        # only current transport event is that session_reset itself; the gen-2
        # client_connected and stale_client_replaced both precede it.
        current_events = [e.get("event") for e in evidence.transport_events]
        if current_events != ["session_reset"]:
            print(f"[FAIL] current transport events wrong: {current_events}")
            return 1

        # stale_client_replaced (and the gen-2 client_connected) must be routed
        # to the historical partition.
        historical_events = [e.get("event") for e in historical.transport_events]
        if "stale_client_replaced" not in historical_events:
            print(f"[FAIL] stale_client_replaced not routed to historical: {historical_events}")
            return 1

        # Version lock fails closed: the three v2 lines (all pre-boundary).
        total_mismatch = len(evidence.version_mismatch) + len(historical.version_mismatch)
        if total_mismatch != 3:
            print(f"[FAIL] expected 3 version mismatches total, got {total_mismatch}")
            return 1

        write_bundle(out_dir, sample_log, sample_config, sample_px4, sample_px4_t0, sample_px4_t5, sample_px4_t30)
        summary = (out_dir / "README.md").read_text(encoding="utf-8")
        boundary = json.loads((out_dir / "session_boundary.json").read_text(encoding="utf-8"))
        historical_excerpts = (out_dir / "historical" / "xplane_px4xplane_excerpts.log").read_text(encoding="utf-8")

        required = [
            "Boundary status: complete-session",
            "Current transport_generation: 2",
            "HIL_SENSOR send-rate mean from log: 28.00 Hz",
            "Offset captures: t0@scheduled:2026-06-15T22:15:03Z",
            "Capture command sets: t0:launcher-timed-px4-diagnostics-v1",
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
        if boundary.get("transport_generation") != 2 or boundary.get("status") != "complete-session":
            print(f"[FAIL] session_boundary.json wrong: {boundary}")
            return 1
        if "stale_client_replaced" not in historical_excerpts:
            print("[FAIL] stale_client_replaced excerpt missing from historical partition")
            return 1

    # --- Part 3: incomplete-session fallback -----------------------------------
    # A latest transport_generation with client_connected but no following
    # session_reset falls back to the client_connected line as the boundary.
    with tempfile.TemporaryDirectory(prefix="px4xplane-hitl-diag-incomplete-") as tmp:
        tmp_path = Path(tmp)
        incomplete_log = tmp_path / "Log.txt"
        sample_config = tmp_path / "config.ini"
        incomplete_out = tmp_path / "bundle"
        incomplete_log.write_text(
            "\n".join(
                [
                    'px4xplane: [TRANSPORT_EVENT] {"diag_version":1,"wall_time_usec":100,"event":"client_connected","transport_generation":4,"estimated_fps":48.0,"flight_loop_counter":100}',
                    "px4xplane: [RATE] diag_version=1 generation=4 wall_time_usec=200 sim_time=1.0s HIL_SENSOR window_msgs=1000 total_msgs=1000 wall_window_sec=40.000 rate_hz=31.00 target_hz=60 estimated_fps=48.0 paused=0 dt_p50_bucket_usec=45000 dt_p95_bucket_usec=60000 dt_max_usec=61000",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        sample_config.write_text(REPO_CONFIG.read_text(encoding="utf-8-sig", errors="replace"), encoding="utf-8")
        write_bundle(incomplete_out, incomplete_log, sample_config, None)
        incomplete_boundary = json.loads((incomplete_out / "session_boundary.json").read_text(encoding="utf-8"))
        if incomplete_boundary.get("status") != "incomplete-session" or incomplete_boundary.get("current_start_line") != 1:
            print(f"[FAIL] hitl-diagnostic-bundle-self-test incomplete boundary: {incomplete_boundary}")
            return 1

    # --- Part 4: hostile stale_client_replaced ordering ------------------------
    # Regression for the Kimi stale-routing blocker: parse_log must route any
    # supported-version stale_client_replaced event to the historical partition
    # unconditionally, even when a hostile/malformed log places it AFTER the
    # current-session boundary line (where the line-number partition alone would
    # wrongly count it as current evidence). Invariant under test:
    # stale_client_replaced lands in historical, never in current.
    def transport_line(event: str, generation: int, usec: int) -> str:
        return (
            'px4xplane: [TRANSPORT_EVENT] '
            f'{{"diag_version":1,"wall_time_usec":{usec},"event":"{event}",'
            f'"transport_generation":{generation},"estimated_fps":50.0,'
            '"flight_loop_counter":1}'
        )

    hostile_cases = {
        # Case A: gen-1 connect+reset, gen-2 connect+reset (establishes the
        # boundary), then a gen-2 stale_client_replaced emitted AFTER the
        # boundary line.
        "A": [
            transport_line("client_connected", 1, 100),
            transport_line("session_reset", 1, 110),
            transport_line("client_connected", 2, 200),
            transport_line("session_reset", 2, 210),
            transport_line("stale_client_replaced", 2, 220),
        ],
        # Case B: gen-1 connect+reset, gen-2 connect (NO reset -> the boundary
        # falls back to the client_connected line), then a gen-2
        # stale_client_replaced AFTER it.
        "B": [
            transport_line("client_connected", 1, 100),
            transport_line("session_reset", 1, 110),
            transport_line("client_connected", 2, 200),
            transport_line("stale_client_replaced", 2, 220),
        ],
    }

    for case_name, log_lines in hostile_cases.items():
        with tempfile.TemporaryDirectory(prefix=f"px4xplane-hitl-diag-hostile-{case_name}-") as tmp:
            hostile_log = Path(tmp) / "Log.txt"
            hostile_log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

            parsed = parse_log(hostile_log)
            current_events = [e.get("event") for e in parsed.current.transport_events]
            historical_events = [e.get("event") for e in parsed.historical.transport_events]

            if "stale_client_replaced" in current_events:
                print(f"[FAIL] case {case_name}: stale_client_replaced leaked into current: {current_events}")
                return 1
            if "stale_client_replaced" not in historical_events:
                print(f"[FAIL] case {case_name}: stale_client_replaced missing from historical: {historical_events}")
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
    parser.add_argument(
        "--px4-capture-dir",
        type=Path,
        help="Directory containing launcher-generated px4_t0.txt, px4_t5.txt, px4_t30.txt, and timed_capture_manifest.json.",
    )
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
        args.px4_capture_dir,
    )
    print(f"Created HITL cadence evidence bundle: {bundle_dir}")
    if not args.xplane_log.is_file():
        print(f"[WARN] X-Plane log not found: {args.xplane_log}")
    if not args.installed_config.is_file():
        print(f"[WARN] installed config not found: {args.installed_config}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
