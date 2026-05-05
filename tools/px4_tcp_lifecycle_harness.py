#!/usr/bin/env python3
"""Exercise the Sprig px4xplane TCP lifecycle contract without X-Plane.

The real plugin runs inside X-Plane, so this harness models the socket ownership
rules that matter for PX4 HITL on TCP 4560:

- keep one listener alive
- accept a PX4 client
- close dead accepted clients immediately
- prefer a newer accepted client over a stale one
- keep listening after client failure
- close listener and client idempotently on plugin reload/disable

Use an ephemeral port by default. Pass --port 4560 only when X-Plane is not
running and you explicitly want to exercise the operational port.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]


class HarnessFailure(AssertionError):
    """Raised when a lifecycle scenario violates the contract."""


@dataclass
class HarnessServer:
    host: str = "127.0.0.1"
    requested_port: int = 0
    listener: socket.socket | None = None
    client: socket.socket | None = None
    events: list[str] = field(default_factory=list)

    def start(self) -> None:
        if self.listener is not None:
            self.events.append("listening already active")
            return

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.requested_port))
        listener.listen(5)
        listener.setblocking(False)
        self.listener = listener
        self.events.append(f"listening {self.port}")

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

            accepted.settimeout(0.2)
            if self.client is not None:
                self.events.append("stale client closed")
                self._close_client()
            self.client = accepted
            self.events.append(f"PX4 connected {address[0]}:{address[1]}")
            accepted_any = True

        return accepted_any

    def send_payload(self, payload: bytes = b"hil") -> bool:
        if self.client is None:
            self.events.append("send skipped no client")
            return False
        try:
            self.client.sendall(payload)
        except OSError as error:
            self.events.append(f"send failed {error.__class__.__name__}")
            self._close_client()
            self.events.append("waiting for PX4 reconnect")
            return False
        return True

    def receive_once(self) -> bytes:
        if self.client is None:
            return b""
        try:
            data = self.client.recv(4096)
        except socket.timeout:
            return b""
        except OSError as error:
            self.events.append(f"receive failed {error.__class__.__name__}")
            self._close_client()
            self.events.append("waiting for PX4 reconnect")
            return b""

        if data == b"":
            self.events.append("PX4 disconnected peer closed")
            self._close_client()
            self.events.append("waiting for PX4 reconnect")
        return data

    def close(self) -> None:
        self._close_client()
        if self.listener is not None:
            self.listener.close()
            self.listener = None
            self.events.append("listener closed")

    def _close_client(self) -> None:
        if self.client is not None:
            try:
                self.client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.client.close()
            self.client = None
            self.events.append("client closed")


def connect_client(port: int) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=1.0)
    client.settimeout(1.0)
    return client


def require(condition: bool, message: str) -> None:
    if not condition:
        raise HarnessFailure(message)


def scenario_clean_startup(server: HarnessServer) -> None:
    server.start()
    client = connect_client(server.port)
    try:
        require(server.accept_pending(), "server did not accept PX4 client")
        require(server.send_payload(b"sensor"), "server could not send to connected PX4 client")
        require(client.recv(64) == b"sensor", "PX4 client did not receive payload")
    finally:
        client.close()


def scenario_probe_resistance(server: HarnessServer) -> None:
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

    require("stale client closed" in server.events, "server did not report stale client closure")


def scenario_send_failure_reconnect(server: HarnessServer) -> None:
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


def scenario_reload_cleanup(server: HarnessServer) -> None:
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
        replacement.start()
        require(replacement.port == port, "port was not reusable after cleanup")
    finally:
        replacement.close()


SCENARIOS: dict[str, Callable[[HarnessServer], None]] = {
    "clean-startup": scenario_clean_startup,
    "probe-resistance": scenario_probe_resistance,
    "stale-client-replacement": scenario_stale_client_replacement,
    "send-failure-reconnect": scenario_send_failure_reconnect,
    "reload-cleanup": scenario_reload_cleanup,
}


def run_scenario(name: str, *, port: int, emit_json: bool) -> bool:
    server = HarnessServer(requested_port=port)
    started_at = time.monotonic()
    try:
        SCENARIOS[name](server)
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
                print(f"  - {event}")
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
    args = parser.parse_args()

    names = sorted(SCENARIOS) if args.scenario == "all" else [args.scenario]
    ok = True
    for name in names:
        ok = run_scenario(name, port=args.port, emit_json=args.json) and ok

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
