#include "ConnectionManager.h"
#include "MAVLinkManager.h"
#if IBM
#include <winsock2.h>
#include <ws2tcpip.h> // For inet_pton
#pragma comment(lib, "Ws2_32.lib")
#endif
#if LIN || APL
#include <unistd.h>
#include <fcntl.h>      // For fcntl(), F_GETFL, F_SETFL, O_NONBLOCK
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#endif
#include "XPLMUtilities.h"
#include <cstring>
#include <string>
#include <errno.h> 
#include <cstdio>


#include <fstream>
#include <sstream>
#include "DataRefManager.h"
#include <ConfigManager.h>
#include "ConnectionStatusHUD.h"
#include "TimeManager.h"

// Defined in px4xplane.cpp; recomputes TARGET_*_PERIOD from ConfigManager fields.
// Must be called after ConfigManager::loadConfiguration() so config.ini values
// (e.g. mavlink_gps_rate_hz) actually drive dispatch instead of the static
// defaults baked in at XPluginStart.
extern void initializeMessagePeriods();

#if LIN || APL
#define INVALID_SOCKET -1
#endif

static bool connected = false;

namespace {
constexpr int kMaxNonBlockingSendAttempts = 8;
constexpr int kBackpressureDisconnectThreshold = 120;

struct FlightLoopTimingDiagnostics {
    float elapsedSinceLastCall = 0.0f;
    float elapsedSinceLastFlightLoop = 0.0f;
    int callbackCounter = 0;
    float estimatedFps = 0.0f;
};

struct SessionIoDiagnostics {
    uint64_t sessionStartUsec = 0;
    uint64_t lastOutboundUsec = 0;
    uint64_t lastInboundUsec = 0;
    uint32_t outboundPackets = 0;
    uint32_t inboundPackets = 0;
    uint32_t outboundBytes = 0;
    uint32_t inboundBytes = 0;
    uint32_t lastOutboundMsgId = 0;
    uint32_t lastInboundMsgId = 0;
    int lastOutboundLen = 0;
    int lastInboundLen = 0;
};

struct TransportSessionState {
    uint32_t generation = 0;
    std::string resetCause = "none";
    std::string lastDisconnectReason = "none";
    bool listenerActive = false;
    bool clientActive = false;
    uint32_t consecutiveBackpressureEvents = 0;
    uint32_t consecutiveRetryLimitEvents = 0;
};

SessionIoDiagnostics g_sessionIoDiagnostics;
FlightLoopTimingDiagnostics g_flightLoopTimingDiagnostics;
TransportSessionState g_transportSessionState;

const char* mavlinkMessageLabel(uint32_t msgid) {
    switch (msgid) {
    case 0:
        return "HEARTBEAT";
    case 65:
        return "RC_CHANNELS";
    case 90:
        return "HIL_ACTUATOR_CONTROLS";
    case 107:
        return "HIL_SENSOR";
    case 113:
        return "HIL_GPS";
    case 115:
        return "HIL_STATE_QUATERNION";
    default:
        return "msg";
    }
}

std::string socketErrorLabel(int errorCode) {
#if IBM
    return std::to_string(errorCode);
#else
    return strerror(errorCode);
#endif
}

uint32_t decodeMavlinkMessageId(const uint8_t* buffer, int len) {
    if (buffer == nullptr || len < 6) {
        return 0;
    }

    if (buffer[0] == 0xFE) {
        return static_cast<uint32_t>(buffer[5]);
    }

    if (buffer[0] == 0xFD && len >= 10) {
        return static_cast<uint32_t>(buffer[7])
            | (static_cast<uint32_t>(buffer[8]) << 8)
            | (static_cast<uint32_t>(buffer[9]) << 16);
    }

    return 0;
}

void resetSessionIoDiagnostics() {
    g_sessionIoDiagnostics = {};
    g_sessionIoDiagnostics.sessionStartUsec = TimeManager::getCurrentTimeUsec();
    g_transportSessionState.consecutiveBackpressureEvents = 0;
    g_transportSessionState.consecutiveRetryLimitEvents = 0;
}

void noteOutboundPacket(const uint8_t* buffer, int len) {
    g_sessionIoDiagnostics.lastOutboundUsec = TimeManager::getCurrentTimeUsec();
    g_sessionIoDiagnostics.outboundPackets++;
    g_sessionIoDiagnostics.outboundBytes += static_cast<uint32_t>(len > 0 ? len : 0);
    g_sessionIoDiagnostics.lastOutboundLen = len;
    g_sessionIoDiagnostics.lastOutboundMsgId = decodeMavlinkMessageId(buffer, len);
}

void noteInboundPacket(uint32_t msgid, int payloadLen) {
    g_sessionIoDiagnostics.lastInboundUsec = TimeManager::getCurrentTimeUsec();
    g_sessionIoDiagnostics.inboundPackets++;
    g_sessionIoDiagnostics.inboundBytes += static_cast<uint32_t>(payloadLen > 0 ? payloadLen : 0);
    g_sessionIoDiagnostics.lastInboundLen = payloadLen;
    g_sessionIoDiagnostics.lastInboundMsgId = msgid;
}

void logSessionIoSnapshot(const char* context, int errorCode) {
    const uint64_t nowUsec = TimeManager::getCurrentTimeUsec();
    const uint64_t sinceStartMs = g_sessionIoDiagnostics.sessionStartUsec > 0
        ? (nowUsec - g_sessionIoDiagnostics.sessionStartUsec) / 1000
        : 0;
    const uint64_t sinceOutboundMs = g_sessionIoDiagnostics.lastOutboundUsec > 0
        ? (nowUsec - g_sessionIoDiagnostics.lastOutboundUsec) / 1000
        : 0;
    const uint64_t sinceInboundMs = g_sessionIoDiagnostics.lastInboundUsec > 0
        ? (nowUsec - g_sessionIoDiagnostics.lastInboundUsec) / 1000
        : 0;

    char buf[768];
    snprintf(
        buf,
        sizeof(buf),
        "px4xplane: IO snapshot at %s: socket_error=%d (%s), session_age_ms=%llu, outbound_packets=%u outbound_bytes=%u last_outbound=%s(%u)/len=%d age_ms=%llu, inbound_packets=%u inbound_bytes=%u last_inbound=%s(%u)/len=%d age_ms=%llu\n",
        context,
        errorCode,
        socketErrorLabel(errorCode).c_str(),
        static_cast<unsigned long long>(sinceStartMs),
        g_sessionIoDiagnostics.outboundPackets,
        g_sessionIoDiagnostics.outboundBytes,
        mavlinkMessageLabel(g_sessionIoDiagnostics.lastOutboundMsgId),
        g_sessionIoDiagnostics.lastOutboundMsgId,
        g_sessionIoDiagnostics.lastOutboundLen,
        static_cast<unsigned long long>(sinceOutboundMs),
        g_sessionIoDiagnostics.inboundPackets,
        g_sessionIoDiagnostics.inboundBytes,
        mavlinkMessageLabel(g_sessionIoDiagnostics.lastInboundMsgId),
        g_sessionIoDiagnostics.lastInboundMsgId,
        g_sessionIoDiagnostics.lastInboundLen,
        static_cast<unsigned long long>(sinceInboundMs));
    XPLMDebugString(buf);
}

std::string escapeJson(const std::string& text) {
    std::string escaped;
    escaped.reserve(text.size() + 8);
    for (char c : text) {
        switch (c) {
        case '\\':
            escaped += "\\\\";
            break;
        case '"':
            escaped += "\\\"";
            break;
        case '\n':
            escaped += "\\n";
            break;
        case '\r':
            escaped += "\\r";
            break;
        case '\t':
            escaped += "\\t";
            break;
        default:
            escaped += c;
            break;
        }
    }
    return escaped;
}

std::string buildTransportSessionEventJson(
    const std::string& eventType,
    const std::string& cause,
    int socketErrorCode) {
    const uint64_t nowUsec = TimeManager::getCurrentTimeUsec();
    const uint64_t sessionAgeMs = g_sessionIoDiagnostics.sessionStartUsec > 0
        ? (nowUsec - g_sessionIoDiagnostics.sessionStartUsec) / 1000
        : 0;

    std::ostringstream json;
    json << "{"
         << "\"diag_version\":1,"
         << "\"wall_time_usec\":" << nowUsec << ","
         << "\"event\":\"" << escapeJson(eventType) << "\","
         // transport_generation is the transport-session reset counter, distinct from the
         // sim/reset `generation` (g_sessionResetGeneration) emitted by
         // [RATE]/[TIMESTAMP]/[TIMESTAMP_SUMMARY]. Do NOT join across grammars on this field.
         << "\"transport_generation\":" << g_transportSessionState.generation << ","
         << "\"reset_cause\":\"" << escapeJson(g_transportSessionState.resetCause) << "\","
         << "\"cause\":\"" << escapeJson(cause) << "\","
         << "\"socket_error_code\":" << socketErrorCode << ","
         << "\"socket_error_label\":\"" << escapeJson(socketErrorLabel(socketErrorCode)) << "\","
         << "\"session_age_ms\":" << sessionAgeMs << ","
         << "\"connected\":" << (connected ? "true" : "false") << ","
         << "\"listener_active\":" << (g_transportSessionState.listenerActive ? "true" : "false") << ","
         << "\"client_active\":" << (g_transportSessionState.clientActive ? "true" : "false") << ","
         << "\"waiting_for_reconnect\":" << ((g_transportSessionState.listenerActive && !connected) ? "true" : "false") << ","
         << "\"outbound_packets\":" << g_sessionIoDiagnostics.outboundPackets << ","
         << "\"outbound_bytes\":" << g_sessionIoDiagnostics.outboundBytes << ","
         << "\"inbound_packets\":" << g_sessionIoDiagnostics.inboundPackets << ","
         << "\"inbound_bytes\":" << g_sessionIoDiagnostics.inboundBytes << ","
         << "\"last_outbound_msgid\":" << g_sessionIoDiagnostics.lastOutboundMsgId << ","
         << "\"last_inbound_msgid\":" << g_sessionIoDiagnostics.lastInboundMsgId << ","
         << "\"last_outbound_len\":" << g_sessionIoDiagnostics.lastOutboundLen << ","
         << "\"last_inbound_len\":" << g_sessionIoDiagnostics.lastInboundLen << ","
         << "\"consecutive_backpressure_events\":" << g_transportSessionState.consecutiveBackpressureEvents << ","
         << "\"consecutive_retry_limit_events\":" << g_transportSessionState.consecutiveRetryLimitEvents << ","
         << "\"flight_loop_elapsed_sec\":" << g_flightLoopTimingDiagnostics.elapsedSinceLastCall << ","
         << "\"flight_loop_elapsed_since_last_sec\":" << g_flightLoopTimingDiagnostics.elapsedSinceLastFlightLoop << ","
         << "\"flight_loop_counter\":" << g_flightLoopTimingDiagnostics.callbackCounter << ","
         << "\"estimated_fps\":" << g_flightLoopTimingDiagnostics.estimatedFps
         << "}";
    return json.str();
}

void emitTransportSessionEvent(
    const std::string& eventType,
    const std::string& cause,
    int socketErrorCode = 0) {
    const std::string payload = buildTransportSessionEventJson(eventType, cause, socketErrorCode);
    std::string logLine = "px4xplane: [TRANSPORT_EVENT] " + payload + "\n";
    XPLMDebugString(logLine.c_str());
}
}

std::map<int, int> ConnectionManager::motorMappings;
int ConnectionManager::sockfd = -1;
int ConnectionManager::newsockfd = -1;

int ConnectionManager::sitlPort = 4560;
std::string ConnectionManager::status = "Disconnected";
std::string ConnectionManager::lastMessage = "";

int ConnectionManager::getLastSocketError() {
#if IBM
    return WSAGetLastError();
#else
    return errno;
#endif
}

bool ConnectionManager::isSendBackpressureError(int errorCode) {
#if IBM
    return errorCode == WSAEWOULDBLOCK;
#else
    return errorCode == EWOULDBLOCK || errorCode == EAGAIN;
#endif
}

bool ConnectionManager::isSocketInterrupted(int errorCode) {
#if IBM
    return errorCode == WSAEINTR;
#else
    return errorCode == EINTR;
#endif
}

std::string ConnectionManager::getSocketErrorString(int errorCode) {
#if IBM
    return std::to_string(errorCode);
#else
    return strerror(errorCode);
#endif
}

std::string ConnectionManager::getSocketErrorString() {
    return getSocketErrorString(getLastSocketError());
}

bool ConnectionManager::configureAcceptedSocket(int clientSock) {
    if (clientSock == INVALID_SOCKET) {
        return false;
    }

#if APL
    int noSigPipe = 1;
    setsockopt(clientSock, SOL_SOCKET, SO_NOSIGPIPE,
               reinterpret_cast<const char*>(&noSigPipe), sizeof(noSigPipe));
#endif

#if IBM
    u_long mode = 1;
    if (ioctlsocket(clientSock, FIONBIO, &mode) != 0) {
        return false;
    }
#elif LIN || APL
    int flags = fcntl(clientSock, F_GETFL, 0);
    if (flags < 0 || fcntl(clientSock, F_SETFL, flags | O_NONBLOCK) < 0) {
        return false;
    }
#endif

    return true;
}

#if IBM
bool ConnectionManager::initializeWinSock() {
    WSADATA wsaData;
    if (WSAStartup(MAKEWORD(2, 2), &wsaData) != 0) {
        XPLMDebugString("px4xplane: Could not initialize Winsock.\n");
        return false;
    }
    return true;
}
#endif


void ConnectionManager::setupServerSocket() {
    XPLMDebugString("px4xplane: Sprig px4xplane setting up TCP listener...\n");

    if (sockfd != -1) {
        XPLMDebugString("px4xplane: TCP listener already active on port 4560.\n");
        return;
    }

    sockfd = socket(AF_INET, SOCK_STREAM, 0);
    if (sockfd < 0) {
        XPLMDebugString("px4xplane: Error opening socket.\n");
        status = "Socket Error";
        setLastMessage("Failed to create socket. System error.");
        XPLMSpeakString("Socket creation failed");
        return;
    }

    // CRITICAL FIX (January 2025): Allow immediate port reuse after disconnect
    // Without this, port 4560 enters TIME_WAIT state and remains unavailable for 30-60s
    // This caused "nothing happens" bug when user tried to reconnect quickly
    int reuse = 1;
    if (setsockopt(sockfd, SOL_SOCKET, SO_REUSEADDR,
                   (const char*)&reuse, sizeof(reuse)) < 0) {
        XPLMDebugString("px4xplane: Warning - could not set SO_REUSEADDR\n");
        // Continue anyway - not critical enough to abort
    }

#ifdef SO_REUSEPORT  // Linux/Mac have this, Windows doesn't
    if (setsockopt(sockfd, SOL_SOCKET, SO_REUSEPORT,
                   (const char*)&reuse, sizeof(reuse)) < 0) {
        // Ignore - not available on all platforms
    }
#endif

    sockaddr_in serv_addr{};
    serv_addr.sin_family = AF_INET;
    serv_addr.sin_addr.s_addr = INADDR_ANY;
    serv_addr.sin_port = htons(sitlPort);

    if (bind(sockfd, (struct sockaddr*)&serv_addr, sizeof(serv_addr)) < 0) {
        XPLMDebugString("px4xplane: Error on binding port 4560.\n");

        // UX FIX: Notify user of error instead of silent failure
        status = "Bind Error";
        setLastMessage("Failed to bind port 4560. Port may be in use by another program.");
        XPLMSpeakString("Port bind failed");

        closeSocket(sockfd);
        sockfd = -1;
        return;
    }

    if (listen(sockfd, 5) < 0) {
        XPLMDebugString("px4xplane: Error on listen.\n");
        closeSocket(sockfd);
        return;
    }

    // CRITICAL FIX (January 2025): Make socket non-blocking
    // BEFORE: accept() was blocking → X-Plane froze until PX4 connected
    // AFTER: Non-blocking socket → poll in flight loop → no freezing
#if IBM
    u_long mode = 1;  // 1 = non-blocking, 0 = blocking
    if (ioctlsocket(sockfd, FIONBIO, &mode) != 0) {
        XPLMDebugString("px4xplane: Error setting socket to non-blocking mode.\n");
        closeSocket(sockfd);
        return;
    }
#elif LIN || APL
    int flags = fcntl(sockfd, F_GETFL, 0);
    if (flags < 0 || fcntl(sockfd, F_SETFL, flags | O_NONBLOCK) < 0) {
        XPLMDebugString("px4xplane: Error setting socket to non-blocking mode.\n");
        closeSocket(sockfd);
        return;
    }
#endif

    // UX FIX (January 2025): Update status for user visibility
    status = "Waiting for PX4 SITL...";
    setLastMessage("Server socket ready on port 4560. Start PX4 SITL to connect.");
    g_transportSessionState.listenerActive = true;
    g_transportSessionState.clientActive = false;

    XPLMDebugString("px4xplane: TCP listening on port 4560; waiting for PX4 reconnect/client.\n");
    emitTransportSessionEvent("listener_ready", "tcp_listener_started");
    XPLMSpeakString("Waiting for PX4 connection");  // Audio feedback

    // NOTE: Don't call acceptConnection() here anymore - poll in flight loop instead

}


/**
 * @brief Non-blocking poll for incoming PX4 connection.
 *
 * CRITICAL FIX (January 2025): Non-blocking connection accept
 *
 * BEFORE: acceptConnection() used blocking accept() → X-Plane froze
 * AFTER: tryAcceptConnection() polls non-blocking socket → no freeze
 *
 * This function is called every flight loop frame when waiting for connection.
 * Returns immediately if no connection available (EWOULDBLOCK/EAGAIN).
 * Only accepts and initializes when PX4 actually connects.
 */
void ConnectionManager::tryAcceptConnection() {

    if (sockfd == -1) {
        return;  // No listener
    }

    int acceptedSock = INVALID_SOCKET;
    int latestAcceptedSock = INVALID_SOCKET;
    sockaddr_in latestCliAddr{};
    bool acceptedAny = false;

    while (true) {
        sockaddr_in cli_addr{};
        socklen_t clilen = sizeof(cli_addr);
        acceptedSock = accept(sockfd, (struct sockaddr*)&cli_addr, &clilen);

        if (acceptedSock < 0) {
            // Check if it's "no connection yet" (not an error) or real error
#if IBM
            int err = WSAGetLastError();
            if (err == WSAEWOULDBLOCK) {
                break;
            }
            XPLMDebugString("px4xplane: Error on accept (Windows error code: ");
            char errBuf[32];
            snprintf(errBuf, sizeof(errBuf), "%d)\n", err);
            XPLMDebugString(errBuf);
#elif LIN || APL
            if (errno == EWOULDBLOCK || errno == EAGAIN) {
                break;
            }
            XPLMDebugString("px4xplane: Error on accept.\n");
#endif
            if (latestAcceptedSock != INVALID_SOCKET) {
                closeSocket(latestAcceptedSock);
            }
            return;
        }

        acceptedAny = true;
        if (latestAcceptedSock != INVALID_SOCKET) {
            closeSocket(latestAcceptedSock);
        }
        latestAcceptedSock = acceptedSock;
        latestCliAddr = cli_addr;
    }

    if (!acceptedAny || latestAcceptedSock == INVALID_SOCKET) {
        return;
    }

    char clientAddr[64] = "unknown";
#if LIN || APL
    inet_ntop(AF_INET, &latestCliAddr.sin_addr, clientAddr, sizeof(clientAddr));
#elif IBM
    InetNtopA(AF_INET, &latestCliAddr.sin_addr, clientAddr, sizeof(clientAddr));
#endif

    acceptedSock = latestAcceptedSock;

    if (!configureAcceptedSocket(acceptedSock)) {
        XPLMDebugString("px4xplane: Failed to configure accepted PX4 client socket; closing it.\n");
        closeSocket(acceptedSock);
        return;
    }

    if (connected || newsockfd != INVALID_SOCKET) {
        char staleBuf[256];
        snprintf(staleBuf, sizeof(staleBuf),
                 "px4xplane: Replacing existing PX4 client with newest pending connection from %s:%d.\n",
                 clientAddr, ntohs(latestCliAddr.sin_port));
        XPLMDebugString(staleBuf);
        emitTransportSessionEvent("stale_client_replaced", "newer_px4_client_accepted");
        handleClientDisconnect("Stale PX4 client closed for newer connection.", true);
    }

    newsockfd = acceptedSock;
    g_transportSessionState.generation += 1;
    g_transportSessionState.resetCause = "client_accept";
    g_transportSessionState.clientActive = true;
    resetSessionIoDiagnostics();

    // Successfully connected!
    char connectedBuf[256];
    snprintf(connectedBuf, sizeof(connectedBuf),
             "px4xplane: PX4 connected from %s:%d.\n",
             clientAddr, ntohs(latestCliAddr.sin_port));
    XPLMDebugString(connectedBuf);
    connected = true;
    emitTransportSessionEvent("client_connected", "px4_tcp_client_accepted");

    // A newly accepted TCP socket is a new PX4 simulator session. Reset all
    // session-scoped timing, parser, sequence, and actuator state before the
    // flight loop sends the first frame. Keep accelerometer calibration across
    // reconnects; recalibrating during PX4 startup can delay EKF accel-bias
    // convergence and block arming.
    extern void resetFlightLoopTimers();  // Defined in px4xplane.cpp
    resetFlightLoopTimers();
    DataRefManager::resetActuatorValues();
    MAVLinkManager::reset(false);
    XPLMDebugString("px4xplane: PX4 session reset complete after client accept.\n");
    emitTransportSessionEvent("session_reset", "client_accept_reset");

    // UX FIX (January 2025): Update status and notify user
    status = "PX4 connected";
    setLastMessage("PX4 connected; HIL messages active.");
    XPLMSpeakString("PX4 connected");  // Audio feedback

    // CRITICAL: Update menu to show "Disconnect from SITL"
    extern void updateMenuItems();  // Defined in px4xplane.cpp
    updateMenuItems();

    DataRefManager::enableOverride();

     DataRefManager::initializeMagneticField();
     XPLMDebugString("px4xplane: Init Magnetic Done.\n");

     // Initialize magnetic field with current aircraft position
     GeodeticPosition initialPosition = {
         DataRefManager::getFloat("sim/flightmodel/position/latitude"),
         DataRefManager::getFloat("sim/flightmodel/position/longitude"),
         DataRefManager::getFloat("sim/flightmodel/position/elevation")
     };

     DataRefManager::updateEarthMagneticFieldNED(initialPosition);
     DataRefManager::lastPosition = initialPosition;
     XPLMDebugString("px4xplane: Magnetic field initialized at current position.\n");

     // Load motor mappings from config.ini
     ConfigManager::loadConfiguration();
     XPLMDebugString("px4xplane: Motor mappings loaded from config.ini.\n");

     // Refresh TARGET_*_PERIOD now that config.ini values are loaded.
     // Without this, periods stay at the defaults set by initializeMessagePeriods()
     // during XPluginStart (e.g. GPS=10Hz default ignores mavlink_gps_rate_hz=20).
     initializeMessagePeriods();

     // Debug: Log loaded configuration
     std::string debugMsg = "Config Name: " + ConfigManager::getConfigName();
     XPLMDebugString(debugMsg.c_str());

}


//not working now ... cannot read ini file...
std::map<int, int> ConnectionManager::loadMotorMappings(const std::string& filename) {
    std::map<int, int> motorMappings;
    std::ifstream file(filename);

    if (!file) {
        XPLMDebugString("Error: Unable to open file ");
        XPLMDebugString(filename.c_str());
        XPLMDebugString("\n");
        return motorMappings;
    }

    std::string line;
    while (std::getline(file, line)) {
        // Ignore comments and empty lines
        if (line.empty() || line[0] == '#') continue;

        std::istringstream iss(line);
        std::string key, equals, value;

        if (!(iss >> key >> equals >> value) || equals != "=") {
            XPLMDebugString("Warning: Ignoring malformed line: ");
            XPLMDebugString(line.c_str());
            XPLMDebugString("\n");
            continue;
        }

        // Check if the key is a PX4 motor
        if (key.substr(0, 4) == "PX4_") {
            int px4Motor = std::stoi(key.substr(4));
            int xplaneMotor = std::stoi(value);
            motorMappings[px4Motor] = xplaneMotor;
            XPLMDebugString("Loaded mapping: PX4 motor ");
            XPLMDebugString(std::to_string(px4Motor).c_str());
            XPLMDebugString(" -> X-Plane motor ");
            XPLMDebugString(std::to_string(xplaneMotor).c_str());
            XPLMDebugString("\n");
        }
    }

    if (motorMappings.empty()) {
        XPLMDebugString("Warning: No motor mappings loaded from ");
        XPLMDebugString(filename.c_str());
        XPLMDebugString("\n");
    }

    return motorMappings;
}




void ConnectionManager::disconnect() {
    bool hadClient = connected || newsockfd != INVALID_SOCKET;
    bool hadListener = sockfd != INVALID_SOCKET;

    // CRITICAL FIX (January 2025): Reset state BEFORE closing sockets
    // Order matters:
    //   1. Zero actuator values (prevents ghost commands)
    //   2. Clear MAVLink command history
    //   3. Disable override flags
    //   4. Close sockets

    if (hadClient) {
        DataRefManager::resetActuatorValues();  // Zero all throttle/control surface datarefs
        MAVLinkManager::reset();                 // Clear actuator command history
        DataRefManager::disableOverride();       // Disable override flags
    }

    closeSocket(sockfd);
    sockfd = -1;
    closeSocket(newsockfd); // Close the newsockfd
    newsockfd = -1;

    connected = false;
    g_transportSessionState.listenerActive = false;
    g_transportSessionState.clientActive = false;
    status = "Disconnected";
    setLastMessage("Disconnected from PX4; TCP listener closed.");
    g_transportSessionState.lastDisconnectReason = "manual_disconnect";
    if (hadClient || hadListener) {
        XPLMDebugString("px4xplane: PX4 disconnected; TCP listener and client sockets closed.\n");
        emitTransportSessionEvent("listener_closed", "manual_disconnect");
    }

    // UX FIX (January 2025): Update menu and notify user
    if (hadClient || hadListener) {
        XPLMSpeakString("Disconnected");  // Audio feedback
    }
    extern void updateMenuItems();  // Defined in px4xplane.cpp
    updateMenuItems();  // Change menu back to "Connect to SITL"
}

void ConnectionManager::closeClient(const std::string& reason) {
    handleClientDisconnect(reason, true);
}

void ConnectionManager::handleClientDisconnect(const std::string& reason, bool resetAircraftState) {
    bool hadClient = connected || newsockfd != INVALID_SOCKET;
    g_transportSessionState.resetCause = resetAircraftState ? "client_disconnect_reset" : "client_disconnect_no_reset";
    g_transportSessionState.lastDisconnectReason = reason;

    if (resetAircraftState && hadClient) {
        extern void resetFlightLoopTimers();  // Defined in px4xplane.cpp
        resetFlightLoopTimers();
        DataRefManager::resetActuatorValues();
        MAVLinkManager::reset(false);
        DataRefManager::disableOverride();
    }

    closeSocket(newsockfd);
    newsockfd = -1;
    connected = false;
    g_transportSessionState.clientActive = false;

    if (sockfd != INVALID_SOCKET) {
        status = "Waiting for PX4 reconnect";
        setLastMessage(reason + " Waiting for PX4 reconnect on TCP 4560.");
        XPLMDebugString(("px4xplane: PX4 disconnected: " + reason + "\n").c_str());
        XPLMDebugString("px4xplane: Waiting for PX4 reconnect on TCP 4560.\n");
        ConnectionStatusHUD::updateStatus(ConnectionStatusHUD::Status::CONN_ERROR, reason);
        emitTransportSessionEvent("waiting_for_reconnect", reason);
    } else {
        status = "Disconnected";
        setLastMessage(reason);
        XPLMDebugString(("px4xplane: PX4 disconnected: " + reason + "\n").c_str());
        emitTransportSessionEvent("client_disconnected", reason);
    }

    resetSessionIoDiagnostics();

    extern void updateMenuItems();  // Defined in px4xplane.cpp
    updateMenuItems();
}

void ConnectionManager::closeSocket(int& sockfd) {
    if (sockfd != INVALID_SOCKET) {
#if IBM
        closesocket(sockfd);
#elif LIN || APL
        close(sockfd);
#endif
        sockfd = -1; // set to -1 to indicate that the socket is no longer valid
    }



}
void ConnectionManager::sendData(const uint8_t* buffer, int len) {
    if (!connected) return;
    if (newsockfd == INVALID_SOCKET) return;
    noteOutboundPacket(buffer, len);

    // Log the MAVLink packet in an interpretable format
    //std::string logMessage = "Sending MAVLink packet: ";
    /*for (int i = 0; i < len; i++) {
        logMessage += std::to_string(buffer[i]) + " ";
    }*/
    //XPLMDebugString(logMessage.c_str());

    int totalBytesSent = 0;
    int attempts = 0;
    while (totalBytesSent < len && attempts < kMaxNonBlockingSendAttempts) {
        ++attempts;
        int sendFlags = 0;
#if LIN
        sendFlags = MSG_NOSIGNAL;
#endif
        int bytesSent = send(newsockfd, reinterpret_cast<const char*>(buffer) + totalBytesSent, len - totalBytesSent, sendFlags);

        if (bytesSent < 0) {
            int errorCode = getLastSocketError();
            if (isSocketInterrupted(errorCode)) {
                continue;
            }

            if (isSendBackpressureError(errorCode)) {
                g_transportSessionState.consecutiveBackpressureEvents++;
                if (g_transportSessionState.consecutiveBackpressureEvents == 1
                    || g_transportSessionState.consecutiveBackpressureEvents % 20 == 0) {
                    char buf[256];
                    snprintf(buf, sizeof(buf),
                             "px4xplane: Send backpressure from PX4 client; dropping this frame (count=%d).\n",
                             g_transportSessionState.consecutiveBackpressureEvents);
                    XPLMDebugString(buf);
                    emitTransportSessionEvent("send_backpressure", "px4_client_not_reading", errorCode);
                }
                if (g_transportSessionState.consecutiveBackpressureEvents >= kBackpressureDisconnectThreshold) {
                    handleClientDisconnect("send backpressure persisted; PX4 TCP client stopped reading.", true);
                }
                return;
            }

            g_transportSessionState.consecutiveBackpressureEvents = 0;
            g_transportSessionState.consecutiveRetryLimitEvents = 0;
            logSessionIoSnapshot("send failure", errorCode);
            emitTransportSessionEvent("send_failure", "socket_send_failed", errorCode);
            char buf[256];
#if IBM
            snprintf(buf, sizeof(buf), "px4xplane: Error sending data: %d\n", errorCode);
#elif LIN || APL
            snprintf(buf, sizeof(buf), "px4xplane: Error sending data: %s\n", getSocketErrorString(errorCode).c_str());
#endif
            XPLMDebugString(buf);
            handleClientDisconnect("send failed: " + getSocketErrorString(errorCode), true);
            return;
        }
        else if (bytesSent == 0) {
            // The peer has closed the connection.
            logSessionIoSnapshot("send returned zero bytes", 0);
            emitTransportSessionEvent("send_zero_bytes", "peer_closed_during_send");
            handleClientDisconnect("send returned zero bytes; PX4 client closed.", true);
            return;
        }

        g_transportSessionState.consecutiveBackpressureEvents = 0;
        g_transportSessionState.consecutiveRetryLimitEvents = 0;
        totalBytesSent += bytesSent;
    }

    if (totalBytesSent < len) {
        g_transportSessionState.consecutiveRetryLimitEvents++;
        if (g_transportSessionState.consecutiveRetryLimitEvents == 1
            || g_transportSessionState.consecutiveRetryLimitEvents % 20 == 0) {
            char buf[256];
            snprintf(buf, sizeof(buf),
                     "px4xplane: Non-blocking send retry limit exceeded; dropping this frame (count=%d).\n",
                     g_transportSessionState.consecutiveRetryLimitEvents);
            XPLMDebugString(buf);
            emitTransportSessionEvent("send_retry_limit", "non_blocking_send_retry_limit", 0);
        }
        if (g_transportSessionState.consecutiveRetryLimitEvents >= kBackpressureDisconnectThreshold) {
            handleClientDisconnect("send backpressure persisted after retry limit.", true);
        }
    } else {
        g_transportSessionState.consecutiveBackpressureEvents = 0;
        g_transportSessionState.consecutiveRetryLimitEvents = 0;
    }
}


void ConnectionManager::receiveData() {
    if (!connected) return;

    uint8_t buffer[256];
    memset(buffer, 0, sizeof(buffer));

    // Set up the read set and timeout for select
    fd_set readSet;
    FD_ZERO(&readSet);
    FD_SET(newsockfd, &readSet);
    struct timeval timeout;
    timeout.tv_sec = 0; // Zero seconds
    timeout.tv_usec = 0; // Zero microseconds

    // Use select to check if there is data available to read
    int result = select(newsockfd + 1, &readSet, NULL, NULL, &timeout);
    if (result < 0) {
        int errorCode = getLastSocketError();
        if (isSocketInterrupted(errorCode) || isSendBackpressureError(errorCode)) {
            return;
        }
        emitTransportSessionEvent("receive_select_failure", "socket_select_failed", errorCode);
        handleClientDisconnect("select failed while reading PX4: " + getSocketErrorString(), true);
    }
    else if (result > 0 && FD_ISSET(newsockfd, &readSet)) {
        // There is data available to read
        int bytesReceived = recv(newsockfd, reinterpret_cast<char*>(buffer), sizeof(buffer) - 1, 0);
        if (bytesReceived < 0) {
            int errorCode = getLastSocketError();
            if (isSocketInterrupted(errorCode) || isSendBackpressureError(errorCode)) {
                return;
            }
            logSessionIoSnapshot("receive failure", errorCode);
            emitTransportSessionEvent("receive_failure", "socket_receive_failed", errorCode);
            handleClientDisconnect("receive failed: " + getSocketErrorString(), true);
        }
        else if (bytesReceived > 0) {
            buffer[bytesReceived] = '\0'; // Null-terminate the received data
            setLastMessage("Receiving from PX4!"); // Store the received message
            //XPLMDebugString("px4xplane: Data received: ");
            //XPLMDebugString(reinterpret_cast<char*>(buffer)); // Write the received message to the X-Plane log

            // Call MAVLinkManager::receiveHILActuatorControls() function here
            MAVLinkManager::receiveHILActuatorControls(buffer, bytesReceived);
        }
        else {
            logSessionIoSnapshot("receive returned zero bytes", 0);
            emitTransportSessionEvent("receive_zero_bytes", "peer_closed_during_receive");
            handleClientDisconnect("PX4 client closed the TCP connection.", true);
        }
    }
}

void ConnectionManager::noteInboundMavlinkMessage(uint32_t msgid, int payloadLen) {
    noteInboundPacket(msgid, payloadLen);
}

void ConnectionManager::noteFlightLoopTiming(float elapsedSinceLastCall, float elapsedSinceLastFlightLoop, int counter) {
    g_flightLoopTimingDiagnostics.elapsedSinceLastCall = elapsedSinceLastCall;
    g_flightLoopTimingDiagnostics.elapsedSinceLastFlightLoop = elapsedSinceLastFlightLoop;
    g_flightLoopTimingDiagnostics.callbackCounter = counter;
    g_flightLoopTimingDiagnostics.estimatedFps =
        elapsedSinceLastCall > 0.0f ? (1.0f / elapsedSinceLastCall) : 0.0f;
}




bool ConnectionManager::isConnected() {
    return connected;
}

/**
 * @brief Check if socket is listening but not yet connected.
 *
 * Returns true when server socket is set up and waiting for PX4 to connect.
 * Used by flight loop to know when to poll for incoming connections.
 *
 * @return true if waiting for connection, false otherwise
 */
bool ConnectionManager::isWaitingForConnection() {
    return (sockfd != -1 && !connected);
}

const std::string& ConnectionManager::getStatus() {
    return status;
}

void ConnectionManager::setLastMessage(const std::string& message) {
    lastMessage = message;
}

const std::string& ConnectionManager::getLastMessage() {
    return lastMessage;
}
#if IBM
void ConnectionManager::cleanupWinSock() {
    WSACleanup();
}
#endif
