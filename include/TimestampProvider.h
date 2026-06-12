#pragma once
#ifndef TIMESTAMPPROVIDER_H
#define TIMESTAMPPROVIDER_H

#include <cstdint>
#include <chrono>
#include <cstddef>

/**
 * @brief Provides high-precision monotonic timestamps for HIL MAVLink messages.
 *
 * PROBLEM SOLVED:
 * X-Plane's total_flight_time_sec and total_running_time_sec datarefs can be
 * quantized at about 1 Hz in X-Plane 12 HITL runs. PX4 lockstep adopts HIL
 * timestamps as simulation time, so those datarefs must not drive HIL message
 * timestamps.
 *
 * SOLUTION:
 * Track time using the X-Plane flight-loop callback delta
 * (inElapsedSinceLastCall), accumulated into a session-scoped 64-bit
 * microsecond simulation-step clock. This keeps HIL_SENSOR, HIL_GPS,
 * HIL_STATE_QUATERNION, and HIL_RC_INPUTS coherent within a callback and
 * aligned with the scheduler clock.
 *
 * USAGE:
 *   uint64_t timestamp = TimestampProvider::getTimestampUsec();
 *   // Use in HIL_SENSOR, HIL_GPS, HIL_STATE_QUATERNION, HIL_RC_INPUTS
 *
 * Thread Safety: NOT thread-safe. All calls must be from X-Plane flight loop thread.
 *
 * @see https://github.com/PX4/PX4-Autopilot/issues/13968 (EKF2 timestamp issue)
 */
class TimestampProvider {
public:
    enum class AdvanceBranch {
        SUB_FRAME_OR_ZERO_DELTA,
        NORMAL_DELTA,
        MAX_DELTA_CAP
    };

    enum class MessageKind {
        HIL_SENSOR = 0,
        HIL_GPS,
        HIL_STATE_QUATERNION,
        HIL_RC_INPUTS,
        COUNT
    };

    struct DeltaStats {
        uint64_t sample_count = 0;
        uint64_t last_timestamp_usec = 0;
        uint64_t last_delta_usec = 0;
        uint64_t min_delta_usec = 0;
        uint64_t max_delta_usec = 0;
        double mean_delta_usec = 0.0;
        uint64_t under_1000_usec_count = 0;
        uint64_t hist_under_1000_usec = 0;
        uint64_t hist_1000_to_5000_usec = 0;
        uint64_t hist_5000_to_15000_usec = 0;
        uint64_t hist_15000_to_25000_usec = 0;
        uint64_t hist_25000_to_35000_usec = 0;
        uint64_t hist_35000_to_45000_usec = 0;
        uint64_t hist_45000_to_60000_usec = 0;
        uint64_t hist_60000_to_100000_usec = 0;
        uint64_t hist_over_100000_usec = 0;
    };

    struct Diagnostics {
        AdvanceBranch last_branch = AdvanceBranch::SUB_FRAME_OR_ZERO_DELTA;
        double raw_total_flight_time_sec = 0.0;
        double computed_delta_sec = 0.0;
        uint64_t simulation_clock_usec = 1000000;
        uint64_t last_delta_usec = 0;
        int64_t drift_usec = 0;
        uint64_t sub_frame_branch_count = 0;
        uint64_t normal_delta_branch_count = 0;
        uint64_t max_delta_cap_branch_count = 0;
        uint64_t capped_large_delta_count = 0;
        DeltaStats message_stats[static_cast<size_t>(MessageKind::COUNT)];
    };

    /**
     * @brief Advance the session simulation-step clock from one flight-loop delta.
     *
     * This must be called once per X-Plane flight-loop callback before any HIL
     * messages are scheduled/sent. raw_total_flight_time_sec is recorded only
     * for diagnostics and never drives timestamp generation.
     */
    static void advanceSimulationClock(double elapsed_since_last_call_sec,
                                       double raw_total_flight_time_sec = 0.0);

    /**
     * @brief Get current timestamp in microseconds for HIL messages.
     *
     * Returns a monotonically increasing timestamp that progresses at
     * simulation time rate, suitable for PX4 lockstep synchronization.
     * Starts from a 1 second base offset on first call after initialization/reset.
     *
     * @return uint64_t Timestamp in microseconds since connection start
     */
    static uint64_t getTimestampUsec();

    /**
     * @brief Record a sent HIL message timestamp for delta diagnostics.
     */
    static void noteMessageTimestamp(MessageKind kind, uint64_t timestamp_usec);

    /**
     * @brief Reset all timing state.
     *
     * Call this on disconnect/reconnect to ensure clean timestamp
     * generation on next connection. Next getTimestampUsec() will return
     * the 1 second base offset.
     */
    static void reset();

    /**
     * @brief Get diagnostic information about timestamp state.
     *
     * @param out_drift_usec Estimated drift between accumulated time and system time
     * @param out_last_delta_usec Last frame delta in microseconds
     */
    static void getDiagnostics(int64_t& out_drift_usec, uint64_t& out_last_delta_usec);

    /**
     * @brief Get the full timestamp diagnostic snapshot.
     */
    static Diagnostics getDiagnostics();

private:
    // High-resolution clock is diagnostic only; it never drives HIL timestamps.
    using SteadyClock = std::chrono::steady_clock;
    using TimePoint = std::chrono::time_point<SteadyClock>;

    static constexpr uint64_t BASE_OFFSET_USEC = 1000000;
    static constexpr uint64_t MAX_DELTA_USEC = 100000;

    static bool s_initialized;
    static bool s_hasAdvancedThisSession;
    static TimePoint s_baseTimePoint;
    static uint64_t s_simulationClockUsec;

    static Diagnostics s_diagnostics;

    static void initializeIfNeeded();
    static void resetDiagnostics();
};

#endif // TIMESTAMPPROVIDER_H
