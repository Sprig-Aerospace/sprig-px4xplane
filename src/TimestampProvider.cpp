/**
 * @file TimestampProvider.cpp
 * @brief Session-scoped simulation-step timestamp generation for PX4 HIL messages.
 */

#include "TimestampProvider.h"
#include "ConfigManager.h"
#include "TimeManager.h"
#include "XPLMUtilities.h"

#include <cmath>
#include <cstdio>

bool TimestampProvider::s_initialized = false;
bool TimestampProvider::s_hasAdvancedThisSession = false;
TimestampProvider::TimePoint TimestampProvider::s_baseTimePoint;
uint64_t TimestampProvider::s_sessionStartWallUsec = 0;
uint64_t TimestampProvider::s_simulationClockUsec = TimestampProvider::BASE_OFFSET_USEC;
// Initial session generation must match MAVLinkManager::g_sessionResetGeneration's
// initial value (1) so the [TIMESTAMP] grammar reports the same per-session
// generation as [RATE]/[TIMESTAMP_SUMMARY] before the first MAVLinkManager::reset().
// After reset, setDiagnosticsGeneration() is the single authority for both grammars.
uint32_t TimestampProvider::s_generation = 1;
TimestampProvider::Diagnostics TimestampProvider::s_diagnostics;

void TimestampProvider::initializeIfNeeded() {
    if (s_initialized) {
        return;
    }

    s_baseTimePoint = SteadyClock::now();
    s_sessionStartWallUsec = TimeManager::getCurrentTimeUsec();
    s_simulationClockUsec = BASE_OFFSET_USEC;
    resetDiagnostics();
    s_initialized = true;

    if (ConfigManager::debug_log_sensor_timing) {
        char buf[256];
        snprintf(buf, sizeof(buf),
            "px4xplane: [TIMESTAMP] diag_version=1 generation=%u wall_time_usec=%llu event=clock_initialized base_usec=%llu drift=unmeasured\n",
            s_generation,
            (unsigned long long)s_diagnostics.wall_time_usec,
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
        updateDriftMeasurement();
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
    updateDriftMeasurement();

    if (ConfigManager::debug_log_sensor_timing) {
        static uint64_t lastLogUsec = 0;
        if (s_simulationClockUsec == BASE_OFFSET_USEC ||
            s_simulationClockUsec - lastLogUsec >= 10000000 ||
            s_diagnostics.last_branch == AdvanceBranch::MAX_DELTA_CAP) {
            const char* branchName =
                (s_diagnostics.last_branch == AdvanceBranch::MAX_DELTA_CAP) ? "max-delta-cap" :
                (s_diagnostics.last_branch == AdvanceBranch::NORMAL_DELTA) ? "normal-delta" :
                "sub-frame/min-delta";
            // Mirror [TIMESTAMP_SUMMARY] (MAVLinkManager.cpp): never fabricate a
            // numeric drift when it is unmeasured; emit the literal "unmeasured".
            char driftBuf[64];
            if (s_diagnostics.drift_measured) {
                snprintf(driftBuf, sizeof(driftBuf), "%+.3f", s_diagnostics.drift_usec / 1000.0);
            } else {
                snprintf(driftBuf, sizeof(driftBuf), "unmeasured");
            }
            char buf[512];
            snprintf(buf, sizeof(buf),
                "px4xplane: [TIMESTAMP] diag_version=1 generation=%u wall_time_usec=%llu event=clock_advance branch=%s raw_flight=%.6f delta_sec=%.6f step_clock_sec=%.6f last_dt_usec=%llu capped=%llu drift_ms=%s\n",
                s_generation,
                (unsigned long long)s_diagnostics.wall_time_usec,
                branchName,
                s_diagnostics.raw_total_flight_time_sec,
                s_diagnostics.computed_delta_sec,
                s_diagnostics.simulation_clock_usec / 1e6,
                (unsigned long long)s_diagnostics.last_delta_usec,
                (unsigned long long)s_diagnostics.capped_large_delta_count,
                driftBuf);
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

void TimestampProvider::setDiagnosticsGeneration(uint32_t generation) {
    s_generation = generation;
    s_diagnostics.generation = generation;
}

uint64_t TimestampProvider::estimatePercentileUsec(const DeltaStats& stats, double percentile) {
    if (stats.sample_count <= 1) {
        return 0;
    }

    const uint64_t deltaSamples = stats.sample_count - 1;
    uint64_t rank = static_cast<uint64_t>(std::ceil((percentile / 100.0) * static_cast<double>(deltaSamples)));
    if (rank == 0) {
        rank = 1;
    }

    struct Bucket {
        uint64_t count;
        uint64_t upper_bound_usec;
    };

    const Bucket buckets[] = {
        {stats.hist_under_1000_usec, 1000},
        {stats.hist_1000_to_5000_usec, 5000},
        {stats.hist_5000_to_15000_usec, 15000},
        {stats.hist_15000_to_25000_usec, 25000},
        {stats.hist_25000_to_35000_usec, 35000},
        {stats.hist_35000_to_45000_usec, 45000},
        {stats.hist_45000_to_60000_usec, 60000},
        {stats.hist_60000_to_100000_usec, 100000},
        {stats.hist_over_100000_usec, stats.max_delta_usec},
    };

    uint64_t cumulative = 0;
    for (const Bucket& bucket : buckets) {
        cumulative += bucket.count;
        if (cumulative >= rank) {
            return bucket.upper_bound_usec;
        }
    }

    return stats.max_delta_usec;
}

void TimestampProvider::reset() {
    s_initialized = false;
    s_hasAdvancedThisSession = false;
    s_sessionStartWallUsec = 0;
    s_simulationClockUsec = BASE_OFFSET_USEC;
    resetDiagnostics();

    if (ConfigManager::debug_log_sensor_timing) {
        char buf[256];
        snprintf(buf, sizeof(buf),
            "px4xplane: [TIMESTAMP] diag_version=1 generation=%u wall_time_usec=%llu event=clock_reset base_usec=1000000 drift=unmeasured\n",
            s_generation,
            (unsigned long long)s_diagnostics.wall_time_usec);
        XPLMDebugString(buf);
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
    s_diagnostics.generation = s_generation;
    s_diagnostics.wall_time_usec = s_sessionStartWallUsec;
    s_diagnostics.drift_measured = false;
}

void TimestampProvider::updateDriftMeasurement() {
    const uint64_t wallNowUsec = TimeManager::getCurrentTimeUsec();
    s_diagnostics.wall_time_usec = wallNowUsec;
    s_diagnostics.generation = s_generation;

    if (s_sessionStartWallUsec == 0) {
        s_diagnostics.drift_measured = false;
        s_diagnostics.drift_usec = 0;
        return;
    }

    const uint64_t wallElapsedUsec = wallNowUsec - s_sessionStartWallUsec;
    const int64_t expectedClockUsec = static_cast<int64_t>(BASE_OFFSET_USEC + wallElapsedUsec);
    s_diagnostics.drift_usec = static_cast<int64_t>(s_simulationClockUsec) - expectedClockUsec;
    s_diagnostics.drift_measured = true;
}
