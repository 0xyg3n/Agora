#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_DIR="$PROJECT_DIR/agent"
LOG_DIR="$PROJECT_DIR/logs"
COMPAT_CHECK_SCRIPT="$PROJECT_DIR/scripts/check-openclaw-compat.sh"

# Shared config
export LIVEKIT_URL="${LIVEKIT_URL:-ws://localhost:7880}"
export LIVEKIT_API_KEY="${LIVEKIT_API_KEY:-APIsknt45f55b023edf}"
export LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET:-23d6de5835812fd4f73121ea9de0fcec8a54}"
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
pkill -f "multiprocessing.forkserver.*livekit-collab/agent" 2>/dev/null || true
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

mkdir -p "$LOG_DIR"

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

echo "Agents are running. Dispatch happens automatically when someone joins a room."
echo "To stop: pkill -f 'python agent.py'"
