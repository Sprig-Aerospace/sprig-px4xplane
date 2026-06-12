#!/usr/bin/env python3
"""Deterministic bench tests for the HIL simulation-step timestamp clock."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_OFFSET_USEC = 1_000_000
MAX_DELTA_USEC = 100_000


@dataclass
class MessageStats:
    timestamps: list[int] = field(default_factory=list)

    @property
    def deltas(self) -> list[int]:
        return [b - a for a, b in zip(self.timestamps, self.timestamps[1:])]


class SimulationStepClock:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.time_usec = BASE_OFFSET_USEC
        self.has_advanced_this_session = False
        self.raw_total_flight_time_sec = 0.0
        self.last_delta_usec = 0
        self.sub_frame_count = 0
        self.normal_count = 0
        self.cap_count = 0
        self.messages = {
            "sensor": MessageStats(),
            "gps": MessageStats(),
            "state": MessageStats(),
            "rc": MessageStats(),
        }

    def advance(self, elapsed_sec: float, raw_total_flight_time_sec: float = 0.0) -> int:
        self.raw_total_flight_time_sec = raw_total_flight_time_sec
        if not self.has_advanced_this_session:
            self.has_advanced_this_session = True
            self.last_delta_usec = 0
            self.sub_frame_count += 1
            return self.time_usec
        if elapsed_sec <= 0.0 or not math.isfinite(elapsed_sec):
            delta_usec = 0
            self.sub_frame_count += 1
        else:
            delta_usec = round(elapsed_sec * 1_000_000)
            if delta_usec > MAX_DELTA_USEC:
                delta_usec = MAX_DELTA_USEC
                self.cap_count += 1
            else:
                self.normal_count += 1
        self.time_usec += delta_usec
        self.last_delta_usec = delta_usec
        return self.time_usec

    def note(self, kind: str) -> int:
        self.messages[kind].timestamps.append(self.time_usec)
        return self.time_usec


def assert_deltas_near(deltas: list[int], expected_usec: int, tolerance_usec: int, required_ratio: float) -> None:
    assert deltas, "expected at least one timestamp delta"
    good = sum(1 for delta in deltas if abs(delta - expected_usec) <= tolerance_usec)
    ratio = good / len(deltas)
    assert ratio >= required_ratio, (
        f"{good}/{len(deltas)} deltas within {expected_usec}+/-{tolerance_usec} us "
        f"({ratio:.5f}, required {required_ratio:.5f})"
    )
    assert all(delta > 0 for delta in deltas), "timestamps must be strictly monotonic per message stream"


def test_ideal_60_hz() -> None:
    clock = SimulationStepClock()
    for _ in range(6001):
        clock.advance(1.0 / 60.0)
        clock.note("sensor")
    deltas = clock.messages["sensor"].deltas
    assert_deltas_near(deltas, 16_667, 100, 0.999)
    assert not any(delta < 1000 for delta in deltas)


def test_observed_25_hz() -> None:
    clock = SimulationStepClock()
    for _ in range(2000):
        clock.advance(0.040)
        clock.note("sensor")
    assert_deltas_near(clock.messages["sensor"].deltas, 40_000, 100, 0.999)


def test_xplane_quantization_replay_is_ignored() -> None:
    clock = SimulationStepClock()
    raw_flight_time = 0.0
    for frame in range(2000):
        if frame % 25 == 0:
            raw_flight_time += 1.0
        clock.advance(0.040, raw_flight_time)
        clock.note("sensor")
    assert_deltas_near(clock.messages["sensor"].deltas, 40_000, 100, 0.999)


def test_pause_zero_delta_then_resume() -> None:
    clock = SimulationStepClock()
    initial = clock.time_usec
    for _ in range(120):
        clock.advance(0.0)
    assert clock.time_usec == initial
    clock.advance(0.040)
    assert clock.time_usec == initial + 40_000
    assert clock.sub_frame_count == 120


def test_large_stall_is_capped() -> None:
    clock = SimulationStepClock()
    clock.advance(0.040)
    before = clock.time_usec
    after = clock.advance(0.500)
    assert after - before == MAX_DELTA_USEC
    assert clock.cap_count == 1


def test_session_reset_and_back_to_back_reconnect() -> None:
    clock = SimulationStepClock()
    session_a_late = 0
    for _ in range(1000):
        session_a_late = clock.advance(0.040)
        clock.note("sensor")
    assert session_a_late > BASE_OFFSET_USEC + 10_000_000

    clock.reset()
    clock.advance(0.040)
    assert clock.note("sensor") == BASE_OFFSET_USEC
    clock.advance(0.040)
    assert clock.note("sensor") == BASE_OFFSET_USEC + 40_000
    assert clock.messages["sensor"].timestamps[-1] < session_a_late
    assert clock.messages["sensor"].deltas == [40_000]


def test_multi_message_coherence() -> None:
    clock = SimulationStepClock()
    clock.advance(0.040)
    timestamps = [
        clock.note("sensor"),
        clock.note("gps"),
        clock.note("state"),
        clock.note("rc"),
    ]
    assert max(timestamps) - min(timestamps) == 0


def test_timestamp_jitter_disabled_for_hil_timestamps() -> None:
    manager_source = (ROOT / "src" / "MAVLinkManager.cpp").read_text()
    sensor_body = manager_source.split("void MAVLinkManager::sendHILSensor", 1)[1]
    sensor_body = sensor_body.split("void MAVLinkManager::sendHILGPS", 1)[0]
    assert "noiseDistribution_timestamp_jitter" not in sensor_body
    assert "lastSensorTime + 1" not in sensor_body


def test_timestamp_provider_has_no_time_dataref_source() -> None:
    provider_source = (ROOT / "src" / "TimestampProvider.cpp").read_text()
    assert '"sim/time/total_flight_time_sec"' not in provider_source
    assert '"sim/time/total_running_time_sec"' not in provider_source
    assert "XPLMGetDataf" not in provider_source
    assert "XPLMFindDataRef" not in provider_source
    assert "DataRefManager" not in provider_source


def run() -> None:
    tests = [
        test_ideal_60_hz,
        test_observed_25_hz,
        test_xplane_quantization_replay_is_ignored,
        test_pause_zero_delta_then_resume,
        test_large_stall_is_capped,
        test_session_reset_and_back_to_back_reconnect,
        test_multi_message_coherence,
        test_timestamp_jitter_disabled_for_hil_timestamps,
        test_timestamp_provider_has_no_time_dataref_source,
    ]
    for test in tests:
        test()
        print(f"[timestamp-clock-bench] PASS {test.__name__}")


if __name__ == "__main__":
    run()
