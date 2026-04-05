#!/usr/bin/env bash
set -euo pipefail
#
# Kill all agent OS processes + remove zombie agent participants from LiveKit rooms.
#

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_DIR="$PROJECT_DIR/agent"

echo "=== Agent Process + LiveKit Cleanup ==="
echo ""

# Step 1: Kill all OS-level agent processes
echo "--- Killing agent OS processes ---"
count=$(pgrep -fc "python agent.py" 2>/dev/null || echo "0")
if [ "$count" -gt 0 ]; then
    echo "Found $count agent process(es). Killing..."
    pkill -f "python agent.py" 2>/dev/null || true
    sleep 2
    # Force-kill survivors
    pkill -9 -f "python agent.py" 2>/dev/null || true
    # Kill forkserver children too
    pkill -f "multiprocessing.forkserver.*agora/agent" 2>/dev/null || true
    pkill -9 -f "multiprocessing.forkserver.*agora/agent" 2>/dev/null || true
    sleep 1
    echo "Done."
else
    echo "No agent processes running."
fi

# Clean up PID lock files
rm -f "$AGENT_DIR/.locks/"*.pid 2>/dev/null || true
echo ""

# Step 2: Remove zombie participants from LiveKit rooms
echo "--- Removing zombie LiveKit participants ---"

cd "$PROJECT_DIR/frontend"

API_KEY="${LIVEKIT_API_KEY:?LIVEKIT_API_KEY must be set}"
API_SECRET="${LIVEKIT_API_SECRET:?LIVEKIT_API_SECRET must be set}"
LK_URL="${LIVEKIT_HTTP_URL:-http://127.0.0.1:7880}"

node -e "
const sdk = require('livekit-server-sdk');

const api  = '${API_KEY}';
const sec  = '${API_SECRET}';
const url  = '${LK_URL}';
const room = new sdk.RoomServiceClient(url, api, sec);

(async () => {
  const rooms = await room.listRooms();
  if (!rooms.length) { console.log('No rooms found.'); return; }

  let removed = 0;
  for (const r of rooms) {
    const parts = await room.listParticipants(r.name);
    const agents = parts.filter(p => p.identity.startsWith('agent-'));
    if (!agents.length) continue;

    console.log('Room:', r.name, '—', agents.length, 'agent participant(s)');

    for (const p of agents) {
      console.log('  Removing zombie:', p.identity);
      try {
        await room.removeParticipant(r.name, p.identity);
        removed++;
      } catch (e) {
        console.error('  Failed to remove', p.identity, ':', e.message);
      }
    }
  }
  console.log();
  console.log('Done. Removed', removed, 'zombie agent(s).');
})();
"

echo ""
echo "=== Verification ==="
remaining=$(pgrep -c -f "python agent.py" 2>/dev/null || true)
remaining=${remaining:-0}
echo "Agent processes remaining: $remaining"
if [ "$remaining" -gt 0 ]; then
    echo "WARNING: Some processes survived!"
    pgrep -fa "python agent.py" 2>/dev/null || true
fi
