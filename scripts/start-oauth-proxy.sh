#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_DIR="$PROJECT_DIR/agent"
LOG_DIR="$PROJECT_DIR/logs"

# Load env
set -a
source "$PROJECT_DIR/.env"
set +a

mkdir -p "$LOG_DIR"

# Kill existing proxy
pkill -f "oauth_proxy" 2>/dev/null && sleep 1 || true

echo "Starting Anthropic OAuth proxy on port ${OAUTH_PROXY_PORT:-8090}..."

cd "$AGENT_DIR"
source .venv/bin/activate

nohup python oauth_proxy.py > "$LOG_DIR/oauth-proxy.log" 2>&1 &
echo "  PID: $! — log: $LOG_DIR/oauth-proxy.log"

sleep 2

# Health check
if curl -sf http://127.0.0.1:${OAUTH_PROXY_PORT:-8090}/health > /dev/null 2>&1; then
    echo "  Health check: OK"
    curl -s http://127.0.0.1:${OAUTH_PROXY_PORT:-8090}/health
    echo ""
else
    echo "  Health check: FAILED"
    tail -5 "$LOG_DIR/oauth-proxy.log"
fi
