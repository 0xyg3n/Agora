#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_DIR="$PROJECT_DIR/agent"

cd "$AGENT_DIR"
source .venv/bin/activate

exec python -m openclaw_bridge --check "$@"
