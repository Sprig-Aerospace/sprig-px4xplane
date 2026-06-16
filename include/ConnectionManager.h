#pragma once
#include <cstdint>
#include <string>
#include <map>

class ConnectionManager {
public:
    // Capacity of the per-callback recv() staging buffer in receiveData().
    // The full-buffer diagnostic threshold (kReceiveBufferBytes - 1) is
    // derived from this so it tracks the actual buffer size rather than a
    // magic literal. See repair #1 in HITL_DIAGNOSTICS.md.
    static constexpr int kReceiveBufferBytes = 256;

    struct ReceiveDataCallbackDiagnostics {
        int selectResult = 0;
        int selectErrorCode = 0;
        bool readable = false;
        int bytesReceived = 0;
        int recvErrorCode = 0;
        bool recvZero = false;
        int messagesParsed = 0;
        uint8_t finalParseState = 0;
        bool parseIncomplete = false;
        // "Data readable immediately after recv": a non-blocking select() run
        // right after recv() reported the socket still readable. NEWLY ARRIVED
        // DATA IS POSSIBLE between the two calls, so this is NOT proof of an
        // undrained backlog and is NOT evidence of parser corruption. Read it
        // only as lockstep-latency signal. Surfaced as receive_post_backlog.
        bool postRecvBacklog = false;
    };

    struct ReceiveDataWindowDiagnostics {
        uint64_t callbacks = 0;
        uint64_t selectReadable = 0;
        uint64_t selectNoData = 0;
        uint64_t selectErrors = 0;
        uint64_t recvErrors = 0;
        uint64_t recvZero = 0;
        uint64_t bytesReceived = 0;
        uint64_t maxBytesReceived = 0;
        uint64_t messagesParsed = 0;
        uint64_t parseIncompleteCallbacks = 0;
        // Count of callbacks where data was readable immediately after recv
        // (see ReceiveDataCallbackDiagnostics::postRecvBacklog). Lockstep-
        // latency signal only; newly arrived data is possible, so this is NOT
        // an undrained backlog and NOT parser corruption.
        uint64_t postRecvBacklogCallbacks = 0;
    };

    static void setupServerSocket();
    static void tryAcceptConnection();  // RENAMED from acceptConnection() - now non-blocking
    static bool isWaitingForConnection();  // NEW: Check if socket ready but not connected
    static void disconnect();
    static void closeSocket(int& sock);
    static void closeClient(const std::string& reason);
    static void sendData(const uint8_t* buffer, int len);
    static void receiveData();
    static ReceiveDataCallbackDiagnostics getLastReceiveDataDiagnostics();
    static ReceiveDataWindowDiagnostics consumeReceiveDataWindowDiagnostics();
    static void noteInboundMavlinkMessage(uint32_t msgid, int payloadLen);
    static void noteFlightLoopTiming(float elapsedSinceLastCall, float elapsedSinceLastFlightLoop, int counter);
    static bool isConnected();
    static const std::string& getStatus();
    static void setLastMessage(const std::string& message); // Function to set the last message
    static const std::string& getLastMessage(); // Function to get the last message
    static void cleanupWinSock();
    static bool initializeWinSock();
    static std::map<int, int> motorMappings;
    static std::map<int, int> loadMotorMappings(const std::string& filename);

private:
    static int sockfd; // Socket file descriptor
    static int newsockfd; // Declaration of newsockfd
    static std::string status;
    static int sitlPort;
    static std::string lastMessage; // Variable to keep the last message
    static void handleClientDisconnect(const std::string& reason, bool resetAircraftState = true);
    static bool configureAcceptedSocket(int clientSock);
    static int getLastSocketError();
    static bool isSendBackpressureError(int errorCode);
    static bool isSocketInterrupted(int errorCode);
    static std::string getSocketErrorString(int errorCode);
    static std::string getSocketErrorString();

};
