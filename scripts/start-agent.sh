#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_DIR="$PROJECT_DIR/agent"
COMPAT_CHECK_SCRIPT="$PROJECT_DIR/scripts/check-openclaw-compat.sh"

# Load env
set -a
source "$PROJECT_DIR/.env"
set +a

: "${AGENT_NAME:?AGENT_NAME must be set}"

if [ "${SKIP_OPENCLAW_COMPAT_CHECK:-0}" != "1" ]; then
    echo "Checking OpenClaw compatibility for $AGENT_NAME..."
    "$COMPAT_CHECK_SCRIPT" "$AGENT_NAME"
fi

cd "$AGENT_DIR"
source .venv/bin/activate

echo "Starting agent: $AGENT_NAME"
exec python agent.py dev
