#!/usr/bin/env python3
"""Source-level lifecycle contract checks for ConnectionManager/MAVLinkManager.

These checks intentionally inspect the real C++ seam code instead of the
contract-only TCP model harness. They guard the reconnect lifecycle invariants
that are difficult to exercise without X-Plane:

- accepted client sockets are explicitly configured before becoming active
- disconnect paths reset aircraft/MAVLink state before closing sockets
- MAVLink parser/session state is reset across PX4 client lifetimes
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONNECTION_CPP = ROOT / "src" / "ConnectionManager.cpp"
MAVLINK_CPP = ROOT / "src" / "MAVLinkManager.cpp"
MAVLINK_H = ROOT / "include" / "MAVLinkManager.h"
PX4XPLANE_CPP = ROOT / "src" / "px4xplane.cpp"


class ContractFailure(AssertionError):
    """Raised when source code no longer satisfies the lifecycle contract."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    detail: str


def strip_comments_and_literals(source: str) -> str:
    """Remove comments/string literals while preserving offsets and newlines."""
    output: list[str] = []
    i = 0
    length = len(source)
    while i < length:
        current = source[i]
        nxt = source[i + 1] if i + 1 < length else ""

        if current == "/" and nxt == "/":
            output.extend("  ")
            i += 2
            while i < length and source[i] != "\n":
                output.append(" ")
                i += 1
            continue

        if current == "/" and nxt == "*":
            output.extend("  ")
            i += 2
            while i < length - 1 and not (source[i] == "*" and source[i + 1] == "/"):
                output.append("\n" if source[i] == "\n" else " ")
                i += 1
            if i < length - 1:
                output.extend("  ")
                i += 2
            continue

        if current in {'"', "'"}:
            quote = current
            output.append(" ")
            i += 1
            escaped = False
            while i < length:
                char = source[i]
                output.append("\n" if char == "\n" else " ")
                i += 1
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    break
            continue

        output.append(current)
        i += 1

    return "".join(output)


def read_code(path: Path) -> str:
    return strip_comments_and_literals(path.read_text(encoding="utf-8"))


def find_function_body(source: str, signature_pattern: str) -> str:
    match = re.search(signature_pattern, source)
    if not match:
        raise ContractFailure(f"missing function matching {signature_pattern!r}")

    brace_start = source.find("{", match.end())
    if brace_start < 0:
        raise ContractFailure(f"missing function body for {signature_pattern!r}")

    depth = 0
    for index in range(brace_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[brace_start + 1 : index]

    raise ContractFailure(f"unterminated function body for {signature_pattern!r}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractFailure(message)


def require_contains(body: str, needle: str, message: str) -> None:
    require(needle in body, message)


def require_order(body: str, needles: list[str], message: str) -> None:
    positions: list[int] = []
    cursor = 0
    for needle in needles:
        position = body.find(needle, cursor)
        require(position >= 0, f"{message}: missing {needle!r}")
        positions.append(position)
        cursor = position + len(needle)
    require(positions == sorted(positions), message)


def check_accepted_socket_flags(connection_cpp: str) -> CheckResult:
    configure = find_function_body(
        connection_cpp,
        r"\bbool\s+ConnectionManager::configureAcceptedSocket\s*\(\s*int\s+clientSock\s*\)",
    )
    require_contains(
        configure,
        "clientSock == INVALID_SOCKET",
        "accepted socket configuration must reject invalid sockets",
    )
    require_contains(
        configure,
        "SO_NOSIGPIPE",
        "accepted sockets must disable SIGPIPE on Apple builds",
    )
    require_contains(
        configure,
        "ioctlsocket(clientSock, FIONBIO, &mode)",
        "accepted sockets must be made non-blocking on Windows",
    )
    require_order(
        configure,
        ["fcntl(clientSock, F_GETFL, 0)", "fcntl(clientSock, F_SETFL, flags | O_NONBLOCK)"],
        "accepted sockets must preserve existing POSIX flags while adding O_NONBLOCK",
    )

    accept = find_function_body(
        connection_cpp,
        r"\bvoid\s+ConnectionManager::tryAcceptConnection\s*\(\s*\)",
    )
    require_order(
        accept,
        [
            "configureAcceptedSocket(acceptedSock)",
            "newsockfd = acceptedSock",
            "connected = true",
        ],
        "accepted socket must be configured before it becomes the active client",
    )
    require_contains(
        accept,
        "closeSocket(acceptedSock)",
        "failed accepted-socket configuration must close the candidate socket",
    )
    require_order(
        accept,
        [
            "resetFlightLoopTimers();",
            "DataRefManager::resetActuatorValues();",
            "MAVLinkManager::reset(false);",
        ],
        "new PX4 sessions must reset timing, actuator, and MAVLink state",
    )
    return CheckResult("accepted-socket-flags", "active clients are configured non-blocking before assignment")


def check_disconnect_reset_order(connection_cpp: str) -> CheckResult:
    disconnect = find_function_body(
        connection_cpp,
        r"\bvoid\s+ConnectionManager::disconnect\s*\(\s*\)",
    )
    require_order(
        disconnect,
        [
            "DataRefManager::resetActuatorValues();",
            "MAVLinkManager::reset",
            "DataRefManager::disableOverride();",
            "closeSocket(sockfd);",
            "closeSocket(newsockfd);",
            "connected = false;",
        ],
        "explicit disconnect must reset aircraft/MAVLink state before socket closure",
    )

    close_client = find_function_body(
        connection_cpp,
        r"\bvoid\s+ConnectionManager::handleClientDisconnect\s*\(\s*const\s+std::string&\s+reason,\s*bool\s+resetAircraftState\s*\)",
    )
    require_order(
        close_client,
        [
            "resetFlightLoopTimers();",
            "DataRefManager::resetActuatorValues();",
            "MAVLinkManager::reset(false);",
            "DataRefManager::disableOverride();",
            "closeSocket(newsockfd);",
            "connected = false;",
        ],
        "client disconnect must reset session state before closing the accepted socket",
    )
    require_contains(
        close_client,
        "sockfd != INVALID_SOCKET",
        "client disconnect should distinguish listener-backed reconnect mode",
    )
    require_contains(
        close_client,
        "setLastMessage(reason +",
        "client disconnect should carry the reason into reconnect status messaging",
    )
    return CheckResult("disconnect-reset-order", "disconnect paths reset state before closing sockets")


def check_mavlink_parser_session_reset(mavlink_cpp: str, mavlink_h: str) -> CheckResult:
    reset = find_function_body(
        mavlink_cpp,
        r"\bvoid\s+MAVLinkManager::reset\s*\(\s*bool\s+resetCalibration\s*\)",
    )
    direct_parser_reset = "mavlink_reset_channel_status(MAVLINK_COMM_0);" in reset
    helper_parser_reset = "resetMAVLinkChannelForPX4Session(MAVLINK_COMM_0);" in reset
    require(
        direct_parser_reset or helper_parser_reset,
        "MAVLink reset must reset MAVLINK_COMM_0 parser/channel state",
    )
    parser_reset_call = (
        "mavlink_reset_channel_status(MAVLINK_COMM_0);"
        if direct_parser_reset
        else "resetMAVLinkChannelForPX4Session(MAVLINK_COMM_0);"
    )
    require_order(
        reset,
        [
            "hilActuatorControlsData = {};",
            "g_sessionResetGeneration++;",
            "g_firstSensorFrameLogged = false;",
            "g_firstActuatorFrameLogged = false;",
            "TimestampProvider::reset();",
            parser_reset_call,
        ],
        "MAVLink reset must clear actuator data, advance generation, and reset parser state",
    )
    require_contains(
        mavlink_h,
        "static uint32_t getSessionResetGeneration();",
        "MAVLink reset generation must remain observable for tests/diagnostics",
    )
    if helper_parser_reset:
        require_contains(
            mavlink_h,
            "static void resetMAVLinkChannelForPX4Session",
            "MAVLink channel reset helper must remain part of the source-level seam",
        )
        channel_reset = find_function_body(
            mavlink_h,
            r"\binline\s+void\s+MAVLinkManager::resetMAVLinkChannelForPX4Session\s*\(\s*uint8_t\s+chan\s*\)",
        )
        require_order(
            channel_reset,
            [
                "mavlink_get_channel_status(chan)",
                "mavlink_reset_channel_status(chan)",
                "status->msg_received = 0;",
                "status->parse_state = MAVLINK_PARSE_STATE_IDLE;",
                "status->current_rx_seq = 0;",
                "status->current_tx_seq = 0;",
                "status->packet_rx_drop_count = 0;",
                "status->flags = preservedFlags;",
                "status->signing = signing;",
                "status->signing_streams = signingStreams;",
            ],
            "MAVLink channel reset helper must clear parser/session state and restore persistent channel config",
        )

    receive = find_function_body(
        mavlink_cpp,
        r"\bvoid\s+MAVLinkManager::receiveHILActuatorControls\s*\(\s*uint8_t\*\s+buffer,\s*int\s+size\s*\)",
    )
    require_order(
        receive,
        [
            "ConnectionManager::isConnected()",
            "mavlink_parse_char(MAVLINK_COMM_0",
            "handleReceivedMessage(msg);",
        ],
        "MAVLink receive path must parse on the reset channel only while connected",
    )

    generation_guarded_functions = [
        r"\bEigen::Vector3f\s+MAVLinkManager::computeAcceleration\s*\(\s*\)",
        r"\bvoid\s+MAVLinkManager::sendHILSensor\s*\(\s*uint8_t\s+sensor_id(?:\s*=\s*0)?\s*\)",
        r"\bvoid\s+MAVLinkManager::sendHILGPS\s*\(\s*\)",
        r"\bvoid\s+MAVLinkManager::sendHILStateQuaternion\s*\(\s*\)",
        r"\bvoid\s+MAVLinkManager::sendHILRCInputs\s*\(\s*\)",
    ]
    for pattern in generation_guarded_functions:
        body = find_function_body(mavlink_cpp, pattern)
        require_contains(
            body,
            "localResetGeneration != g_sessionResetGeneration",
            f"{pattern!r} must observe session reset generation",
        )
        require_contains(
            body,
            "localResetGeneration = g_sessionResetGeneration",
            f"{pattern!r} must latch session reset generation",
        )

    return CheckResult("mavlink-parser-session-reset", "parser and per-session static state reset together")


def check_transport_session_event_seam(connection_cpp: str, px4xplane_cpp: str) -> CheckResult:
    require_contains(
        connection_cpp,
        "emitTransportSessionEvent(",
        "transport session seam must emit structured transport events",
    )
    accept = find_function_body(
        connection_cpp,
        r"\bvoid\s+ConnectionManager::tryAcceptConnection\s*\(\s*\)",
    )
    require_order(
        accept,
        [
            "g_transportSessionState.generation += 1;",
            "g_transportSessionState.resetCause =",
            "resetSessionIoDiagnostics();",
            "emitTransportSessionEvent(",
        ],
        "accepted clients must advance generation before emitting transport events",
    )
    send = find_function_body(
        connection_cpp,
        r"\bvoid\s+ConnectionManager::sendData\s*\(\s*const\s+uint8_t\*\s+buffer,\s*int\s+len\s*\)",
    )
    require_contains(
        send,
        "g_transportSessionState.consecutiveBackpressureEvents",
        "send path must keep backpressure counters in session state",
    )
    require_contains(
        px4xplane_cpp,
        "ConnectionManager::noteFlightLoopTiming",
        "flight loop must feed timing context into transport session events",
    )
    return CheckResult("transport-session-event-seam", "transport events carry generation, counters, and timing seam")


def run_checks() -> list[CheckResult]:
    connection_cpp = read_code(CONNECTION_CPP)
    mavlink_cpp = read_code(MAVLINK_CPP)
    mavlink_h = read_code(MAVLINK_H)
    px4xplane_cpp = read_code(PX4XPLANE_CPP)
    return [
        check_accepted_socket_flags(connection_cpp),
        check_disconnect_reset_order(connection_cpp),
        check_mavlink_parser_session_reset(mavlink_cpp, mavlink_h),
        check_transport_session_event_seam(connection_cpp, px4xplane_cpp),
    ]


def main() -> int:
    try:
        results = run_checks()
    except ContractFailure as error:
        print(f"[FAIL] source-lifecycle-contract-checks: {error}")
        return 1

    for result in results:
        print(f"[PASS] {result.name}: {result.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
