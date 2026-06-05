#!/usr/bin/env python3
"""Exercise the Sprig px4xplane TCP lifecycle contract without X-Plane.

The real plugin runs inside X-Plane, so this contract-only harness models the
socket ownership rules and structured transport-session events that matter for
PX4 HITL on TCP 4560.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
MAX_NONBLOCKING_SEND_ATTEMPTS = 8
HARNESS_SEND_BUFFER_BYTES = 4096


class HarnessFailure(AssertionError):
    """Raised when a lifecycle scenario violates the contract."""


@dataclass
class HarnessServer:
    host: str = "127.0.0.1"
    requested_port: int = 0
    listener: socket.socket | None = None
    client: socket.socket | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    generation: int = 0
    reset_cause: str = "none"
    started_at: float = field(default_factory=time.monotonic)
    listener_active: bool = False
    client_active: bool = False
    consecutive_backpressure_events: int = 0
    consecutive_retry_limit_events: int = 0
    last_flight_loop_elapsed_sec: float = 0.0
    last_flight_loop_elapsed_since_last_sec: float = 0.0
    last_flight_loop_counter: int = 0

    def emit(
        self,
        event: str,
        cause: str,
        *,
        socket_error_code: int = 0,
        **extra: Any,
    ) -> None:
        elapsed = max(0.0, time.monotonic() - self.started_at)
        payload = {
            "event": event,
            "generation": self.generation,
            "reset_cause": self.reset_cause,
            "cause": cause,
            "socket_error_code": socket_error_code,
            "session_age_ms": int(elapsed * 1000),
            "listener_active": self.listener_active,
            "client_active": self.client_active,
            "waiting_for_reconnect": self.listener_active and not self.client_active,
            "consecutive_backpressure_events": self.consecutive_backpressure_events,
            "consecutive_retry_limit_events": self.consecutive_retry_limit_events,
            "flight_loop_elapsed_sec": self.last_flight_loop_elapsed_sec,
            "flight_loop_elapsed_since_last_sec": self.last_flight_loop_elapsed_since_last_sec,
            "flight_loop_counter": self.last_flight_loop_counter,
            "estimated_fps": (1.0 / self.last_flight_loop_elapsed_sec)
            if self.last_flight_loop_elapsed_sec > 0.0
            else 0.0,
        }
        payload.update(extra)
        self.events.append(payload)

    def note_flight_loop(
        self,
        *,
        elapsed_since_last_call: float,
        elapsed_since_last_flight_loop: float,
        counter: int,
    ) -> None:
        self.last_flight_loop_elapsed_sec = elapsed_since_last_call
        self.last_flight_loop_elapsed_since_last_sec = elapsed_since_last_flight_loop
        self.last_flight_loop_counter = counter

    def reset_session_counters(self) -> None:
        self.consecutive_backpressure_events = 0
        self.consecutive_retry_limit_events = 0

    def start(self) -> None:
        if self.listener is not None:
            self.emit("listener_ready", "listener_already_active")
            return

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.requested_port))
        listener.listen(5)
        listener.setblocking(False)
        self.listener = listener
        self.listener_active = True
        self.client_active = False
        self.emit("listener_ready", "tcp_listener_started", port=self.port)

    @property
    def port(self) -> int:
        if self.listener is None:
            return self.requested_port
        return int(self.listener.getsockname()[1])

    def accept_pending(self, *, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        accepted_any = False

        while time.monotonic() < deadline:
            if self.listener is None:
                return accepted_any
            try:
                accepted, address = self.listener.accept()
            except BlockingIOError:
                if accepted_any:
                    return True
                time.sleep(0.01)
                continue

            accepted.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, HARNESS_SEND_BUFFER_BYTES)
            accepted.setblocking(False)
            if self.client is not None:
                self.emit("stale_client_replaced", "newer_px4_client_accepted")
                self._close_client()
            self.client = accepted
            self.client_active = True
            self.generation += 1
            self.reset_cause = "client_accept"
            self.reset_session_counters()
            self.emit(
                "client_connected",
                "px4_tcp_client_accepted",
                remote=f"{address[0]}:{address[1]}",
            )
            self.emit("session_reset", "client_accept_reset")
            accepted_any = True

        return accepted_any

    def send_payload(self, payload: bytes = b"hil") -> bool:
        if self.client is None:
            self.emit("send_skipped", "no_client")
            return False

        total_sent = 0
        attempts = 0
        view = memoryview(payload)

        while total_sent < len(payload) and attempts < MAX_NONBLOCKING_SEND_ATTEMPTS:
            attempts += 1
            try:
                sent = self.client.send(view[total_sent:])
            except BlockingIOError:
                self.consecutive_backpressure_events += 1
                self.emit("send_backpressure", "px4_client_not_reading")
                self._disconnect_client("send backpressure persisted; PX4 TCP client stopped reading.")
                return False
            except InterruptedError:
                continue
            except OSError as error:
                self.emit(
                    "send_failure",
                    "socket_send_failed",
                    socket_error_code=getattr(error, "errno", 0) or 0,
                    socket_error_label=error.__class__.__name__,
                )
                self._disconnect_client(f"send failed: {error.__class__.__name__}")
                return False

            if sent == 0:
                self.emit("send_zero_bytes", "peer_closed_during_send")
                self._disconnect_client("send returned zero bytes; PX4 client closed.")
                return False

            self.reset_session_counters()
            total_sent += sent

        if total_sent < len(payload):
            self.consecutive_retry_limit_events += 1
            self.emit("send_retry_limit", "non_blocking_send_retry_limit")
            self._disconnect_client("send backpressure persisted after retry limit.")
            return False

        self.reset_session_counters()
        return True

    def receive_once(self) -> bytes:
        if self.client is None:
            return b""
        try:
            data = self.client.recv(4096)
        except (BlockingIOError, socket.timeout):
            return b""
        except OSError as error:
            self.emit(
                "receive_failure",
                "socket_receive_failed",
                socket_error_code=getattr(error, "errno", 0) or 0,
                socket_error_label=error.__class__.__name__,
            )
            self._disconnect_client(f"receive failed: {error.__class__.__name__}")
            return b""

        if data == b"":
            self.emit("receive_zero_bytes", "peer_closed_during_receive")
            self._disconnect_client("PX4 client closed the TCP connection.")
        return data

    def close(self) -> None:
        self._close_client()
        if self.listener is not None:
            self.listener.close()
            self.listener = None
            self.listener_active = False
            self.emit("listener_closed", "manual_disconnect")

    def _disconnect_client(self, reason: str) -> None:
        self.reset_cause = "client_disconnect_reset"
        self.client_active = False
        self._close_client()
        if self.listener is not None:
            self.emit("waiting_for_reconnect", reason)
        else:
            self.emit("client_disconnected", reason)
        self.reset_session_counters()

    def _close_client(self) -> None:
        if self.client is not None:
            try:
                self.client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.client.close()
            self.client = None
        self.client_active = False


def connect_client(port: int) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=1.0)
    client.settimeout(1.0)
    return client


def require(condition: bool, message: str) -> None:
    if not condition:
        raise HarnessFailure(message)


def require_event(server: HarnessServer, event: str) -> dict[str, Any]:
    for payload in server.events:
        if payload["event"] == event:
            return payload
    raise HarnessFailure(f"missing transport event: {event}")


def scenario_clean_startup(server: HarnessServer) -> None:
    server.note_flight_loop(elapsed_since_last_call=0.02, elapsed_since_last_flight_loop=0.02, counter=1)
    server.start()
    client = connect_client(server.port)
    try:
        require(server.accept_pending(), "server did not accept PX4 client")
        require(server.send_payload(b"sensor"), "server could not send to connected PX4 client")
        require(client.recv(64) == b"sensor", "PX4 client did not receive payload")
    finally:
        client.close()


def scenario_probe_resistance(server: HarnessServer) -> None:
    server.note_flight_loop(elapsed_since_last_call=0.05, elapsed_since_last_flight_loop=0.05, counter=2)
    server.start()
    probe = connect_client(server.port)
    require(server.accept_pending(), "server did not accept probe")
    probe.close()

    for _ in range(3):
        server.receive_once()
        if server.client is None:
            break
        time.sleep(0.05)

    require(server.client is None, "closed probe remained as accepted client")
    real_px4 = connect_client(server.port)
    try:
        require(server.accept_pending(), "server did not accept real PX4 after probe")
        require(server.send_payload(b"gps"), "server could not send after probe cleanup")
        require(real_px4.recv(64) == b"gps", "real PX4 did not receive payload after probe")
    finally:
        real_px4.close()


def scenario_stale_client_replacement(server: HarnessServer) -> None:
    server.note_flight_loop(elapsed_since_last_call=0.016, elapsed_since_last_flight_loop=0.016, counter=3)
    server.start()
    old_client = connect_client(server.port)
    new_client = connect_client(server.port)
    try:
        require(server.accept_pending(), "server did not accept old/new clients")
        require(server.send_payload(b"new"), "server could not send to newest client")
        require(new_client.recv(64) == b"new", "newest client did not own HIL lane")
    finally:
        old_client.close()
        new_client.close()

    require_event(server, "stale_client_replaced")


def scenario_send_failure_reconnect(server: HarnessServer) -> None:
    server.note_flight_loop(elapsed_since_last_call=0.02, elapsed_since_last_flight_loop=0.02, counter=4)
    server.start()
    doomed = connect_client(server.port)
    require(server.accept_pending(), "server did not accept doomed client")
    doomed.close()

    failed = False
    for _ in range(5):
        failed = not server.send_payload(b"sensor")
        if failed and server.client is None:
            break
        time.sleep(0.05)

    require(failed, "send failure did not close dead client")
    require(server.listener is not None, "listener closed after client send failure")

    px4 = connect_client(server.port)
    try:
        require(server.accept_pending(), "server did not accept reconnect after send failure")
        require(server.send_payload(b"ok"), "server could not send after reconnect")
        require(px4.recv(64) == b"ok", "reconnected PX4 did not receive payload")
    finally:
        px4.close()


def scenario_backpressure_disconnect(server: HarnessServer) -> None:
    server.note_flight_loop(elapsed_since_last_call=0.01, elapsed_since_last_flight_loop=0.01, counter=5)
    server.start()
    stalled = connect_client(server.port)
    stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, HARNESS_SEND_BUFFER_BYTES)
    require(server.accept_pending(), "server did not accept stalled client")

    try:
        failed = False
        payload = b"x" * 65536
        for _ in range(256):
            failed = not server.send_payload(payload)
            if failed and server.client is None:
                break
            time.sleep(0.001)

        require(failed, "non-reading client did not trigger send backpressure")
        require(server.client is None, "backpressured client remained connected")
        require(server.listener is not None, "listener closed after send backpressure")
        require(
            sum(1 for event in server.events if event["event"] == "send_backpressure") == 1,
            "backpressure disconnect reason was logged more than once",
        )
    finally:
        stalled.close()

    px4 = connect_client(server.port)
    try:
        require(server.accept_pending(), "server did not accept reconnect after backpressure")
        require(server.send_payload(b"ok"), "server could not send after backpressure reconnect")
        require(px4.recv(64) == b"ok", "reconnected PX4 did not receive payload")
    finally:
        px4.close()


def scenario_reload_cleanup(server: HarnessServer) -> None:
    server.note_flight_loop(elapsed_since_last_call=0.033, elapsed_since_last_flight_loop=0.033, counter=6)
    server.start()
    client = connect_client(server.port)
    require(server.accept_pending(), "server did not accept client before cleanup")
    port = server.port
    client.close()

    server.close()
    server.close()
    require(server.listener is None, "listener remained after idempotent close")
    require(server.client is None, "client remained after idempotent close")

    replacement = HarnessServer(requested_port=port)
    try:
        replacement.note_flight_loop(elapsed_since_last_call=0.033, elapsed_since_last_flight_loop=0.033, counter=7)
        replacement.start()
        require(replacement.port == port, "port was not reusable after cleanup")
    finally:
        replacement.close()


SCENARIOS: dict[str, Callable[[HarnessServer], None]] = {
    "backpressure-disconnect": scenario_backpressure_disconnect,
    "clean-startup": scenario_clean_startup,
    "probe-resistance": scenario_probe_resistance,
    "reload-cleanup": scenario_reload_cleanup,
    "send-failure-reconnect": scenario_send_failure_reconnect,
    "stale-client-replacement": scenario_stale_client_replacement,
}


def validate_structured_events(server: HarnessServer, scenario: str) -> None:
    require(server.events, f"{scenario}: expected structured transport events")
    generations = {
        event["generation"]
        for event in server.events
        if event["event"] in {"client_connected", "session_reset"}
    }
    require(generations, f"{scenario}: no connected generations recorded")
    for event in server.events:
        require("generation" in event, f"{scenario}: event missing generation")
        require("cause" in event, f"{scenario}: event missing cause")
        require("reset_cause" in event, f"{scenario}: event missing reset_cause")
        require("listener_active" in event, f"{scenario}: event missing listener_active")
        require("flight_loop_counter" in event, f"{scenario}: event missing flight_loop_counter")

    if scenario == "backpressure-disconnect":
        send_backpressure = require_event(server, "send_backpressure")
        waiting = require_event(server, "waiting_for_reconnect")
        require(send_backpressure["generation"] == waiting["generation"], "backpressure generation changed mid-disconnect")
        require(send_backpressure["consecutive_backpressure_events"] >= 1, "backpressure event missing counter")
        require(waiting["reset_cause"] == "client_disconnect_reset", "backpressure disconnect should mark reset cause")
    elif scenario == "send-failure-reconnect":
        waiting = require_event(server, "waiting_for_reconnect")
        reconnects = [event for event in server.events if event["event"] == "client_connected"]
        require(len(reconnects) >= 2, "send-failure scenario must reconnect into a new generation")
        require(reconnects[0]["generation"] != reconnects[-1]["generation"], "reconnect must advance generation")
        require(waiting["generation"] == reconnects[0]["generation"], "disconnect should stay attached to failed generation")
    elif scenario == "stale-client-replacement":
        stale = require_event(server, "stale_client_replaced")
        reset = require_event(server, "session_reset")
        require(stale["generation"] <= reset["generation"], "stale replacement should precede or match reset generation")


def run_scenario(name: str, *, port: int, emit_json: bool) -> bool:
    server = HarnessServer(requested_port=port)
    started_at = time.monotonic()
    try:
        SCENARIOS[name](server)
        validate_structured_events(server, name)
    except Exception as error:
        result = {
            "scenario": name,
            "ok": False,
            "error": str(error),
            "events": server.events,
            "elapsed_sec": round(time.monotonic() - started_at, 3),
        }
        if emit_json:
            print(json.dumps(result, indent=2))
        else:
            print(f"[FAIL] {name}: {error}")
            for event in server.events:
                print(f"  - {json.dumps(event, sort_keys=True)}")
        return False
    finally:
        server.close()

    result = {
        "scenario": name,
        "ok": True,
        "events": server.events,
        "elapsed_sec": round(time.monotonic() - started_at, 3),
    }
    if emit_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"[PASS] {name}")
    return True


def run_self_test() -> int:
    ok = True
    for name in sorted(SCENARIOS):
        ok = run_scenario(name, port=0, emit_json=False) and ok
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=sorted(SCENARIOS) + ["all"],
        default="all",
        help="Scenario to run.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="TCP port to bind. Defaults to an ephemeral port; use 4560 only when safe.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON result records.")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run all offline transport-session assertions.",
    )
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    names = sorted(SCENARIOS) if args.scenario == "all" else [args.scenario]
    ok = True
    for name in names:
        ok = run_scenario(name, port=args.port, emit_json=args.json) and ok

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
