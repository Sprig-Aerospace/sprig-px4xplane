/**
 * @file TimestampProvider.cpp
 * @brief Session-scoped simulation-step timestamp generation for PX4 HIL messages.
 */

#include "TimestampProvider.h"
#include "ConfigManager.h"
#include "XPLMUtilities.h"

#include <cmath>
#include <cstdio>

bool TimestampProvider::s_initialized = false;
bool TimestampProvider::s_hasAdvancedThisSession = false;
TimestampProvider::TimePoint TimestampProvider::s_baseTimePoint;
uint64_t TimestampProvider::s_simulationClockUsec = TimestampProvider::BASE_OFFSET_USEC;
TimestampProvider::Diagnostics TimestampProvider::s_diagnostics;

void TimestampProvider::initializeIfNeeded() {
    if (s_initialized) {
        return;
    }

    s_baseTimePoint = SteadyClock::now();
    s_simulationClockUsec = BASE_OFFSET_USEC;
    resetDiagnostics();
    s_initialized = true;

    if (ConfigManager::debug_log_sensor_timing) {
        char buf[256];
        snprintf(buf, sizeof(buf),
            "px4xplane: [TIMESTAMP] Simulation-step clock initialized, base=%llu us\n",
            (unsigned long long)s_simulationClockUsec);
        XPLMDebugString(buf);
    }
}

void TimestampProvider::advanceSimulationClock(double elapsed_since_last_call_sec,
                                               double raw_total_flight_time_sec) {
    initializeIfNeeded();

    s_diagnostics.raw_total_flight_time_sec = raw_total_flight_time_sec;
    s_diagnostics.computed_delta_sec = elapsed_since_last_call_sec;

    if (!s_hasAdvancedThisSession) {
        s_hasAdvancedThisSession = true;
        s_diagnostics.last_branch = AdvanceBranch::SUB_FRAME_OR_ZERO_DELTA;
        s_diagnostics.sub_frame_branch_count++;
        s_diagnostics.last_delta_usec = 0;
        s_diagnostics.simulation_clock_usec = s_simulationClockUsec;
        s_diagnostics.drift_usec = 0;
        return;
    }

    uint64_t deltaUsec = 0;
    if (elapsed_since_last_call_sec <= 0.0 || !std::isfinite(elapsed_since_last_call_sec)) {
        s_diagnostics.last_branch = AdvanceBranch::SUB_FRAME_OR_ZERO_DELTA;
        s_diagnostics.sub_frame_branch_count++;
    } else {
        double cappedDeltaSec = elapsed_since_last_call_sec;
        if (cappedDeltaSec > static_cast<double>(MAX_DELTA_USEC) / 1e6) {
            cappedDeltaSec = static_cast<double>(MAX_DELTA_USEC) / 1e6;
            s_diagnostics.last_branch = AdvanceBranch::MAX_DELTA_CAP;
            s_diagnostics.max_delta_cap_branch_count++;
            s_diagnostics.capped_large_delta_count++;
        } else {
            s_diagnostics.last_branch = AdvanceBranch::NORMAL_DELTA;
            s_diagnostics.normal_delta_branch_count++;
        }
        deltaUsec = static_cast<uint64_t>(std::llround(cappedDeltaSec * 1e6));
    }

    s_simulationClockUsec += deltaUsec;
    s_diagnostics.simulation_clock_usec = s_simulationClockUsec;
    s_diagnostics.last_delta_usec = deltaUsec;
    s_diagnostics.drift_usec = 0;

    if (ConfigManager::debug_log_sensor_timing) {
        static uint64_t lastLogUsec = 0;
        if (s_simulationClockUsec == BASE_OFFSET_USEC ||
            s_simulationClockUsec - lastLogUsec >= 10000000 ||
            s_diagnostics.last_branch == AdvanceBranch::MAX_DELTA_CAP) {
            const char* branchName =
                (s_diagnostics.last_branch == AdvanceBranch::MAX_DELTA_CAP) ? "max-delta-cap" :
                (s_diagnostics.last_branch == AdvanceBranch::NORMAL_DELTA) ? "normal-delta" :
                "sub-frame/min-delta";
            char buf[384];
            snprintf(buf, sizeof(buf),
                "px4xplane: [TIMESTAMP] branch=%s raw_flight=%.6f delta=%.6f sec step_clock=%.6f sec last_dt=%llu us capped=%llu drift=%+.3f ms\n",
                branchName,
                s_diagnostics.raw_total_flight_time_sec,
                s_diagnostics.computed_delta_sec,
                s_diagnostics.simulation_clock_usec / 1e6,
                (unsigned long long)s_diagnostics.last_delta_usec,
                (unsigned long long)s_diagnostics.capped_large_delta_count,
                s_diagnostics.drift_usec / 1000.0);
            XPLMDebugString(buf);
            lastLogUsec = s_simulationClockUsec;
        }
    }
}

uint64_t TimestampProvider::getTimestampUsec() {
    initializeIfNeeded();
    return s_simulationClockUsec;
}

void TimestampProvider::noteMessageTimestamp(MessageKind kind, uint64_t timestamp_usec) {
    initializeIfNeeded();

    const size_t index = static_cast<size_t>(kind);
    if (index >= static_cast<size_t>(MessageKind::COUNT)) {
        return;
    }

    DeltaStats& stats = s_diagnostics.message_stats[index];
    if (stats.sample_count > 0) {
        const uint64_t deltaUsec = (timestamp_usec >= stats.last_timestamp_usec)
            ? (timestamp_usec - stats.last_timestamp_usec)
            : 0;
        stats.last_delta_usec = deltaUsec;
        if (stats.sample_count == 1 || deltaUsec < stats.min_delta_usec) {
            stats.min_delta_usec = deltaUsec;
        }
        if (deltaUsec > stats.max_delta_usec) {
            stats.max_delta_usec = deltaUsec;
        }
        const double count = static_cast<double>(stats.sample_count);
        stats.mean_delta_usec += (static_cast<double>(deltaUsec) - stats.mean_delta_usec) / count;
        if (deltaUsec < 1000) {
            stats.under_1000_usec_count++;
        }
        if (deltaUsec < 1000) {
            stats.hist_under_1000_usec++;
        } else if (deltaUsec < 5000) {
            stats.hist_1000_to_5000_usec++;
        } else if (deltaUsec < 15000) {
            stats.hist_5000_to_15000_usec++;
        } else if (deltaUsec < 25000) {
            stats.hist_15000_to_25000_usec++;
        } else if (deltaUsec < 35000) {
            stats.hist_25000_to_35000_usec++;
        } else if (deltaUsec < 45000) {
            stats.hist_35000_to_45000_usec++;
        } else if (deltaUsec < 60000) {
            stats.hist_45000_to_60000_usec++;
        } else if (deltaUsec <= 100000) {
            stats.hist_60000_to_100000_usec++;
        } else {
            stats.hist_over_100000_usec++;
        }
    }
    stats.last_timestamp_usec = timestamp_usec;
    stats.sample_count++;
}

void TimestampProvider::reset() {
    s_initialized = false;
    s_hasAdvancedThisSession = false;
    s_simulationClockUsec = BASE_OFFSET_USEC;
    resetDiagnostics();

    if (ConfigManager::debug_log_sensor_timing) {
        XPLMDebugString("px4xplane: [TIMESTAMP] Simulation-step clock reset - next session starts at 1000000 us\n");
    }
}

void TimestampProvider::getDiagnostics(int64_t& out_drift_usec, uint64_t& out_last_delta_usec) {
    initializeIfNeeded();
    out_drift_usec = s_diagnostics.drift_usec;
    out_last_delta_usec = s_diagnostics.last_delta_usec;
}

TimestampProvider::Diagnostics TimestampProvider::getDiagnostics() {
    initializeIfNeeded();
    return s_diagnostics;
}

void TimestampProvider::resetDiagnostics() {
    s_diagnostics = Diagnostics{};
    s_diagnostics.simulation_clock_usec = s_simulationClockUsec;
}
