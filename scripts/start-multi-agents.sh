#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_DIR="$PROJECT_DIR/agent"
LOG_DIR="$PROJECT_DIR/logs"
COMPAT_CHECK_SCRIPT="$PROJECT_DIR/scripts/check-openclaw-compat.sh"

# Shared config
export LIVEKIT_URL="${LIVEKIT_URL:-ws://localhost:7880}"
export LIVEKIT_API_KEY="${LIVEKIT_API_KEY:?LIVEKIT_API_KEY must be set in .env or environment}"
export LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET:?LIVEKIT_API_SECRET must be set in .env or environment}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-llama3.2:3b}"
export WHISPER_MODEL="${WHISPER_MODEL:-small}"

if [ "${SKIP_OPENCLAW_COMPAT_CHECK:-0}" != "1" ]; then
    echo "Checking OpenClaw compatibility..."
    "$COMPAT_CHECK_SCRIPT" Laira Loki
    echo ""
fi

# Kill ALL existing agent processes (parent workers + forked children)
echo "Stopping ALL existing agent processes..."
pkill -f "python agent.py" 2>/dev/null || true
sleep 1
# Force-kill any survivors
pkill -9 -f "python agent.py" 2>/dev/null || true
# Also kill forkserver children that reference agent paths
pkill -f "multiprocessing.forkserver.*agora/agent\|multiprocessing.forkserver.*livekit-collab/agent" 2>/dev/null || true
sleep 1

# Clean up stale PID files
rm -f "$AGENT_DIR/.locks/"*.pid 2>/dev/null || true

# Verify nothing is left
remaining=$(pgrep -c -f "python agent.py" 2>/dev/null || true)
remaining=${remaining:-0}
if [ "$remaining" -gt 0 ]; then
    echo "WARNING: $remaining agent processes still running after cleanup!"
    pgrep -fa "python agent.py" 2>/dev/null || true
    echo "Force killing with SIGKILL..."
    pkill -9 -f "python agent.py" 2>/dev/null || true
    sleep 2
fi

echo "All clear — no stale agents running."

# Kill any existing ACP bus + injector
pkill -f "python acp_bus.py" 2>/dev/null || true
pkill -f "python acp_bus_injector.py" 2>/dev/null || true
sleep 0.5

mkdir -p "$LOG_DIR"

# Start ACP Event Bus
echo "Starting ACP Event Bus..."
cd "$AGENT_DIR"
source .venv/bin/activate
nohup python acp_bus.py > "$LOG_DIR/acp-bus.log" 2>&1 &
ACP_BUS_PID=$!
echo "  ACP Bus PID: $ACP_BUS_PID — log: $LOG_DIR/acp-bus.log"
sleep 1

# Start ACP Bus → Gateway context injector
echo "Starting ACP Bus injector..."
nohup python acp_bus_injector.py > "$LOG_DIR/acp-injector.log" 2>&1 &
echo "  Injector PID: $! — log: $LOG_DIR/acp-injector.log"

start_agent() {
    local name="$1"
    local voice="$2"
    local logfile="$LOG_DIR/agent-${name,,}.log"

    truncate -s 0 "$logfile" 2>/dev/null || true

    echo "Starting agent: $name (voice: $voice)"

    cd "$AGENT_DIR"
    source .venv/bin/activate

    AGENT_NAME="$name" \
    EDGE_TTS_VOICE="$voice" \
    nohup python agent.py dev > "$logfile" 2>&1 &

    echo "  PID: $! — log: $logfile"
}

# Agent 1: Laira — multilingual voice
start_agent "Laira" "de-DE-SeraphinaMultilingualNeural"

# Small delay so the two workers don't race on port binding
sleep 2

# Agent 2: Loki — English voice
start_agent "Loki" "en-US-GuyNeural"

echo ""
echo "Both agents starting. Waiting for registration..."
sleep 6

echo ""
echo "=== Agent Processes ==="
pgrep -fa "python agent.py" 2>/dev/null || echo "No agent processes found!"
echo ""

echo "=== Agent Logs ==="
for name in laira loki; do
    logfile="$LOG_DIR/agent-${name}.log"
    echo "--- $name ---"
    grep -a "registered worker\|ERROR\|error\|Starting agent\|Claimed PID lock" "$logfile" 2>/dev/null | tail -5
    echo ""
done

echo "=== ACP Event Bus ==="
pgrep -fa "python acp_bus.py" 2>/dev/null || echo "WARNING: ACP Bus not running!"
echo ""

echo "Agents are running. Dispatch happens automatically when someone joins a room."
echo "To stop: pkill -f 'python agent.py'; pkill -f 'python acp_bus.py'"
