# VirtualComms

Self-hosted LiveKit voice and video rooms where humans and OpenClaw-backed AI agents work in the same call.

This repo runs the current Skynet Comms stack: browser UI, token and ops server, LiveKit media server config, and the Python voice agents that bridge room activity into persistent OpenClaw sessions.

## What Runs Here

- Humans join a LiveKit room from the browser with mic, camera, and chat.
- Agents join the same room as LiveKit participants.
- Speech is handled locally with Silero VAD and faster-whisper STT.
- All reasoning goes through OpenClaw sessions, not the LiveKit LLM pipeline.
- Agent replies are spoken with `edge-tts` and echoed into room chat.
- The UI exposes agent telemetry, OpenClaw event traces, a room-scoped thermal feed, and an embedded terminal panel.

## Screenshots

### Pre-join

![Pre-join screen](docs/screenshots/pre-join.png)

### In-call UI

![In-call overview](docs/screenshots/in-call-overview.png)

![In-call stage variation 1](docs/screenshots/in-call-stage-v2.png)

![In-call stage variation 2](docs/screenshots/in-call-stage-v3.png)

![In-call stage variation 3](docs/screenshots/in-call-stage-v4.png)

### Control Detail

![Controls close-up](docs/screenshots/controls-closeup.png)

## Runtime Architecture

```text
Browser
  ├─ Pre-join + in-call UI (React/Vite)
  ├─ Chat, mic, camera, screen share
  └─ Admin actions: call / restart / kick agents
          │
          ▼
frontend/server.ts
  ├─ /api/token
  ├─ /api/agents
  ├─ /api/agent/*
  ├─ /api/observability/*
  └─ /api/terminal/ws
          │
          ▼
LiveKit Server
  ├─ Human participants
  └─ Agent participants
          │
          ▼
agent/agent.py
  ├─ Silero VAD
  ├─ faster-whisper STT
  ├─ NoOp LiveKit LLM shim
  ├─ edge-tts output
  ├─ room-scoped telemetry attrs
  └─ OpenClaw bridge calls
          │
          ▼
agent/openclaw_bridge.py
  ├─ docker exec skynet-{agent} openclaw agent
  ├─ uses --session-id livekit-<room>
  ├─ validates pinned OpenClaw version
  └─ emits observability events
          │
          ▼
OpenClaw container session
  ├─ persistent session JSONL per room
  ├─ full bot personality / memory / tools
  └─ optional thermal timeline via session log parsing
```

## Current Behavior

### Voice and chat flow

```text
Human mic
  -> Silero VAD
  -> faster-whisper STT
  -> agent.py
  -> openclaw_bridge.py
  -> docker exec skynet-{agent} openclaw agent --session-id livekit-<room>
  -> text reply
  -> edge-tts
  -> LiveKit audio back into the room

Human typed chat
  -> same OpenClaw path

Agent-to-agent
  -> lk.agent.chat
  -> recent room context injection
  -> mention-triggered follow-up turns
```

### Vision flow

Vision requests do not go through OpenClaw. The agent captures the active camera or screen-share frame and sends it through the direct vision path in `agent/vision.py`.

### Observability flow

- Live state is surfaced through LiveKit participant attributes:
  - `agent_state`
  - `agent_activity`
  - `agent_status_text`
  - `agent_last_activity_at`
  - `agent_error_text`
- Bridge-level events are posted to `/api/observability/events`.
- Room thermal history is parsed from OpenClaw session JSONL via `/api/observability/thermal/recent`.
- The embedded terminal uses `/api/terminal/ws`.

## Key Capabilities

- Local STT, VAD, and TTS for voice interaction
- OpenClaw-backed reasoning with persistent room-scoped sessions
- Manual agent dispatch by default
- Agent telemetry cards plus 3D stage state
- OpenClaw event feed and thermal monitor tab
- Embedded PTY terminal in the in-call UI
- OpenClaw runtime compatibility checks pinned to `2026.3.13`
- Mention-triggered agent-to-agent coordination
- Token/context controls on replayed room context
- Basic regression coverage for bridge, runtime helpers, and frontend telemetry

## Repository Layout

```text
livekit-collab/
├── agent/
│   ├── agent.py                      # Main LiveKit voice agent
│   ├── openclaw_bridge.py            # OpenClaw CLI bridge + observability
│   ├── openclaw_llm_plugin.py        # No-op LLM shim for AgentSession
│   ├── edge_tts_plugin.py            # TTS integration
│   ├── whisper_stt_plugin.py         # faster-whisper integration
│   ├── vision.py                     # Vision request path
│   ├── runtime_utils.py              # Context/fallback/trigger helpers
│   └── tests/
├── config/
│   └── openclaw-version.txt          # Pinned required OpenClaw version
├── frontend/
│   ├── server.ts                     # Token server + ops + observability + terminal
│   ├── src/
│   │   ├── components/               # VoiceRoom, AgentModel3D, events, terminal
│   │   └── lib/agentTelemetry.ts     # Shared telemetry derivation
│   └── tests/
├── scripts/
│   ├── deploy.sh
│   ├── start-agent.sh
│   ├── start-multi-agents.sh
│   └── check-openclaw-compat.sh
├── server/
│   ├── docker-compose.yml            # LiveKit service
│   └── livekit.yaml                  # LiveKit config
└── README.md
```

## Quick Start

### 1. Prerequisites

- Docker running locally
- LiveKit API key and secret
- OpenClaw bot containers reachable as `skynet-laira` and `skynet-loki`
- Python virtualenv at `agent/.venv`
- Node.js available for the frontend server

### 2. Configure environment

Start from `.env.example` and add the values you actually use. The current stack typically needs:

```bash
LIVEKIT_URL=ws://127.0.0.1:7880
LIVEKIT_HTTP_URL=http://127.0.0.1:7880
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...

ADMIN_API_SECRET=...
AGENT_NAMES=Laira,Loki
AUTO_DISPATCH_ON_JOIN=false

AGENT_NAME=Laira
EDGE_TTS_VOICE=de-DE-SeraphinaMultilingualNeural
WHISPER_MODEL=small
OPENCLAW_REQUIRED_VERSION=2026.3.13
```

Notes:

- `AUTO_DISPATCH_ON_JOIN` is optional. Default behavior in the current UI is manual dispatch.
- `OPENCLAW_REQUIRED_VERSION` may be omitted if you want the repo pin in `config/openclaw-version.txt` to be authoritative.

### 3. Start LiveKit

```bash
cd server
docker compose up -d
```

### 4. Build and serve the frontend

```bash
cd frontend
npm install
npm run build
npx tsx server.ts
```

The token and ops server binds to `127.0.0.1:3210`.

### 5. Start the agents

```bash
./scripts/start-multi-agents.sh
```

Or a single agent:

```bash
AGENT_NAME=Laira ./scripts/start-agent.sh
```

The startup scripts run the OpenClaw compatibility preflight before launching workers.

### 6. Open the app

Local machine:

```text
http://127.0.0.1:3210
```

Remote machine over SSH tunnel:

```bash
ssh -L 3210:127.0.0.1:3210 -L 7880:127.0.0.1:7880 <host>
```

Then open `http://127.0.0.1:3210`.

### 7. Dispatch agents

By default, agents do not auto-join when a human joins a room. Use the in-app `Call` controls, or turn on `AUTO_DISPATCH_ON_JOIN=true`.

## Server Endpoints

### Core room and agent endpoints

- `GET /api/agents`
  - returns configured agent names and whether auto-dispatch-on-join is enabled
- `POST /api/token`
  - issues a room token
- `POST /api/agent/status`
  - returns room-scoped agent snapshots, including last-known offline state
- `POST /api/agent/dispatch`
- `POST /api/agent/dispatch-all`
- `POST /api/agent/restart`
- `POST /api/agent/kick`

### Observability endpoints

- `POST /api/observability/events`
  - receives bridge events from the agents
- `GET /api/observability/events/recent?room=<room>`
  - recent room-scoped OpenClaw lifecycle events
- `GET /api/observability/thermal/recent?room=<room>`
  - parsed thermal-style timeline from session JSONL
- `WS /api/observability/stream?room=<room>`
  - live event stream

### Terminal endpoint

- `WS /api/terminal/ws`
  - PTY-backed terminal used by the in-call UI

## OpenClaw Compatibility

This repo enforces a pinned OpenClaw version for the LiveKit bridge.

- Pin file: `config/openclaw-version.txt`
- Current repo pin: `2026.3.13`
- Manual check:

```bash
./scripts/check-openclaw-compat.sh Laira Loki
```

Important constraints:

- LiveKit room isolation uses `--session-id livekit-<room>`.
- The bridge must not invent a fake OpenClaw `--agent livekit`.
- If the installed OpenClaw binary is older than the config metadata or the repo pin, the compatibility check fails.

## Validation Commands

### Python

```bash
agent/.venv/bin/python -m py_compile agent/agent.py agent/runtime_utils.py agent/openclaw_bridge.py
agent/.venv/bin/python -m unittest agent.tests.test_runtime_utils
agent/.venv/bin/python -m unittest agent.tests.test_openclaw_bridge
```

### Frontend

```bash
cd frontend
npm run test:telemetry
npm run build
```

## Known Caveats

- `frontend/README.md` is now the authoritative frontend-specific doc; the root README stays at the system level.
- `nginx/` is currently empty. `scripts/deploy.sh` expects an operator-provided `nginx/livekit.conf`.
- Legacy provider plugins such as `claude_llm_plugin.py` and `codex_llm_plugin.py` still exist in the repo, but the active runtime path uses OpenClaw as the reasoning layer.
- Long-lived room sessions can still grow expensive if agent-to-agent chatter is left unbounded. The repo includes context caps, but not full session compaction yet.

## License

Proprietary. Copyright (c) 2026 Giannis Zacharioudakis. All rights reserved.
