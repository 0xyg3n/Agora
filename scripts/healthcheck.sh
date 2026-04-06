#!/usr/bin/env bash
# Agora healthcheck — verifies all services are running
set -u

OK=0
WARN=0
FAIL=0

check() {
    local name="$1" status="$2" detail="${3:-}"
    if [ "$status" = "OK" ]; then
        printf "  %-18s %s %s\n" "$name" "[OK]" "$detail"
        OK=$((OK + 1))
    elif [ "$status" = "WARN" ]; then
        printf "  %-18s %s %s\n" "$name" "[WARN]" "$detail"
        WARN=$((WARN + 1))
    else
        printf "  %-18s %s %s\n" "$name" "[FAIL]" "$detail"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== Agora Healthcheck ==="
echo ""

# ACP Event Bus
BUS_PID=$(pgrep -f "python acp_bus.py" 2>/dev/null | head -1)
if [ -n "$BUS_PID" ]; then
    check "ACP Bus" "OK" "PID $BUS_PID"
else
    check "ACP Bus" "FAIL" "not running"
fi

# Agent processes
AGENT_COUNT=$(pgrep -af "python agent.py dev" 2>/dev/null | grep -v bash | wc -l)
if [ "$AGENT_COUNT" -eq 2 ]; then
    check "Agent count" "OK" "$AGENT_COUNT processes"
elif [ "$AGENT_COUNT" -gt 2 ]; then
    check "Agent count" "WARN" "$AGENT_COUNT processes (expected 2)"
else
    check "Agent count" "FAIL" "$AGENT_COUNT processes (expected 2)"
fi

# Laira registration
LAIRA_LOG="${LOGS:-$(dirname "$0")/../logs}/agent-laira.log"
if [ -f "$LAIRA_LOG" ] && grep -q "registered worker" "$LAIRA_LOG" 2>/dev/null; then
    TS=$(grep -a "registered worker" "$LAIRA_LOG" | tail -1 | awk '{print $1}')
    check "Laira" "OK" "registered at $TS"
else
    check "Laira" "FAIL" "not registered"
fi

# Loki registration
LOKI_LOG="${LOGS:-$(dirname "$0")/../logs}/agent-loki.log"
if [ -f "$LOKI_LOG" ] && grep -q "registered worker" "$LOKI_LOG" 2>/dev/null; then
    TS=$(grep -a "registered worker" "$LOKI_LOG" | tail -1 | awk '{print $1}')
    check "Loki" "OK" "registered at $TS"
else
    check "Loki" "FAIL" "not registered"
fi

# LiveKit server
LK_PID=$(pgrep -f "livekit-server" 2>/dev/null | head -1)
if [ -n "$LK_PID" ]; then
    check "LiveKit" "OK" "PID $LK_PID"
else
    check "LiveKit" "FAIL" "not running"
fi

# Frontend
FE_PID=$(pgrep -f "tsx server.ts\|node.*server.js\|npx tsx server" 2>/dev/null | head -1)
if [ -n "$FE_PID" ]; then
    check "Frontend" "OK" "PID $FE_PID"
else
    check "Frontend" "WARN" "not detected"
fi

# Bus connectivity
if [ -n "$BUS_PID" ]; then
    if timeout 3 bash -c 'echo > /dev/tcp/127.0.0.1/9090' 2>/dev/null; then
        check "Bus port 9090" "OK" "accepting connections"
    else
        check "Bus port 9090" "FAIL" "not accepting connections"
    fi
fi

echo ""
echo "Summary: $OK ok, $WARN warnings, $FAIL failures"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
