#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Skynet Comms — Deploy ==="

# 1. Start LiveKit server
echo "[1/4] Starting LiveKit server..."
cd "$PROJECT_DIR/server"
docker compose up -d
echo "  LiveKit server started on port 7880"

# 2. Build frontend
echo "[2/4] Building frontend..."
cd "$PROJECT_DIR/frontend"
npm run build
echo "  Frontend built to dist/"

# 3. Start token server
echo "[3/4] Starting token server..."
cd "$PROJECT_DIR/frontend"
# Source env vars
set -a
source "$PROJECT_DIR/.env"
set +a

# Kill existing token server if running
pkill -f "tsx server.ts" 2>/dev/null || true
sleep 1

nohup npx tsx server.ts > "$PROJECT_DIR/logs/token-server.log" 2>&1 &
echo "  Token server started on port 3210 (PID: $!)"

# 4. Install nginx config
echo "[4/4] Setting up Nginx..."
sudo ln -sf "$PROJECT_DIR/nginx/livekit.conf" /etc/nginx/sites-enabled/livekit.conf
sudo nginx -t && sudo systemctl reload nginx
echo "  Nginx configured and reloaded"

echo ""
echo "=== Deployment complete ==="
echo "  LiveKit:  wss://your-livekit-domain"
echo "  Web UI:   https://your-comms-domain"
echo ""
echo "To start the AI agent:"
echo "  cd $PROJECT_DIR/agent"
echo "  source .venv/bin/activate"
echo "  python agent.py dev"
