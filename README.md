# Agora

Real-time voice rooms where humans and AI agents collaborate across platforms.

Agora is the voice layer of the Skynet Comms stack: a browser-based LiveKit room where humans speak with AI agents (Laira on Hermes, Loki on OpenClaw), with cross-session awareness via the ACP Event Bus connecting voice rooms, Telegram, and Discord into a shared context.

## Architecture

```text
                    ┌─────────────────────────────────────────┐
                    │           ACP Event Bus                  │
                    │      ws://0.0.0.0:9090                  │
                    │  In-memory pub/sub, 100-event ring buf  │
                    └────┬──────────┬──────────┬──────────────┘
                         │          │          │
              subscribe  │  publish │  query   │
                         │          │          │
       ┌─────────────────┼──────────┼──────────┼──────────────────┐
       │                 │          │          │                   │
  ┌────▼────┐      ┌─────▼────┐  ┌─▼──────┐  │  ┌───────────┐   │
  │ Telegram │      │  Agora   │  │ Agora  │  │  │  Discord  │   │
  │ session  │      │  Agent   │  │ Agent  │  │  │  session  │   │
  │ (Hermes) │      │  Laira   │  │  Loki  │  │  │ (Hermes)  │   │
  └──────────┘      └────┬─────┘  └───┬────┘  │  └───────────┘   │
                         │            │        │                   │
                    ACP bridge   ACP bridge     │                   │
                    (SSE stream) (SSE stream)   │                   │
                         │            │        │                   │
                   ┌─────▼────┐  ┌───▼──────┐ │                   │
                   │  Hermes  │  │ OpenClaw │ │                   │
                   │ Gateway  │  │  Gateway │ │                   │
                   │ (Laira)  │  │  (Loki)  │ │                   │
                   └──────────┘  └──────────┘ │                   │
                                              │                   │
       ┌──────────────────────────────────────┘                   │
       │                                                          │
  ┌────▼─────────────────────────────────────────────────────┐    │
  │                    LiveKit Server                         │    │
  │  Human participants + Agent participants (voice/chat)     │    │
  └────▲─────────────────────────────────────────────────────┘    │
       │                                                          │
  ┌────┴──────────┐                                               │
  │   Browser UI  │  React/Vite frontend at :3210                 │
  │   (Agora)     │  mic, camera, chat, agent controls            │
  └───────────────┘                                               │
                                                                  │
  acp_bus_query tool ─── agents query the bus from ANY session ───┘
```

## How It Works

### Voice Flow
```
Human mic → Silero VAD → faster-whisper STT → agent.py
  → ACP bridge → Hermes/OpenClaw gateway → LLM response
  → sentence-by-sentence SSE streaming → edge-tts → LiveKit audio
```

### Cross-Session Awareness
- When someone speaks in the Agora voice room, the event is published to the ACP Event Bus
- Any agent on any platform (Telegram, Discord, Agora) can query the bus using the `acp_bus_query` tool
- This means Laira on Telegram can answer "what happened in the voice room?" by querying the bus

### Agent Architecture
- **Laira** (Hermes gateway): Claude-based, SSE streaming, full tool suite, Telegram/Discord/Agora
- **Loki** (OpenClaw gateway): GPT-based, SSE via API shim, Telegram/Discord/Agora
- Both agents use the ACP bridge for voice room communication and the ACP bus for cross-session context

## ACP Event Bus

The bus is a lightweight WebSocket pub/sub broker (`agent/acp_bus.py`):

- **Topics**: `room:skynet-comms`, `agent:laira`, `agent:loki`, etc.
- **Events**: `{type, speaker, agent, content, ts}`
- **Ring buffer**: Last 100 events per topic (in-memory, no disk)
- **Protocol**: JSON over WebSocket — auth, subscribe, publish, recent

Agents publish voice input and responses. The `acp_bus_query` tool (registered natively in Hermes and as an OpenClaw skill) lets any session query the bus on demand.

## Platform Registration

Agora is registered as a first-class platform in both agent backends:

- **Hermes**: `Platform.AGORA` in the gateway config, platform hint in prompt builder, `X-Hermes-Platform: agora` header on all ACP bridge requests
- **OpenClaw**: Agora awareness in workspace TOOLS.md and HEARTBEAT.md, bus query skill in workspace/skills/

## Adding a New Agent

Edit `agent/agent_registry.py`:

```python
AgentConfig(
    name="NewAgent",
    container="skynet-newagent",
    acp_url="http://172.20.0.X:8642",
    voice="en-US-AriaNeural",
    streaming=True,
)
```

## Quick Start

### 1. Prerequisites
- Docker with agent containers (`skynet-laira`, `skynet-loki`)
- Python venv at `agent/.venv` with `websockets`, `aiohttp`
- Node.js for the frontend
- LiveKit server

### 2. Configure
```bash
cp .env.example .env
# Edit .env with your LiveKit keys and agent URLs
```

### 3. Start everything
```bash
# LiveKit server
cd server && docker compose up -d

# Frontend
cd frontend && npm install && npm run build && npx tsx server.ts &

# Agents + ACP bus (all-in-one)
./scripts/start-multi-agents.sh
```

### 4. Open the app
```
http://127.0.0.1:3210
```

Remote access via SSH tunnel:
```bash
ssh -L 3210:127.0.0.1:3210 -L 7880:127.0.0.1:7880 <host>
```

## Repository Layout

```text
agora/
├── agent/
│   ├── agent.py                # Main Agora voice agent
│   ├── acp_bridge.py           # HTTP streaming bridge to gateways
│   ├── acp_bus.py              # ACP Event Bus server (WebSocket pub/sub)
│   ├── acp_bus_client.py       # Bus client library
│   ├── acp_protocol.py         # ACP message types
│   ├── agent_registry.py       # Centralized agent config
│   ├── openclaw_api_shim.py    # OpenClaw HTTP/SSE shim (deployed in container)
│   ├── openclaw_bridge.py      # Legacy docker exec bridge (fallback)
│   ├── edge_tts_plugin.py      # TTS integration
│   ├── whisper_stt_plugin.py   # faster-whisper STT
│   ├── vision.py               # Vision request path
│   ├── runtime_utils.py        # Context/fallback/trigger helpers
│   └── tests/                  # 57 tests
├── config/
│   └── openclaw-version.txt    # Pinned OpenClaw version
├── frontend/
│   ├── server.ts               # Token server + ops + observability
│   └── src/                    # React UI
├── scripts/
│   ├── start-multi-agents.sh   # Start bus + agents
│   └── check-openclaw-compat.sh
├── server/
│   ├── docker-compose.yml
│   └── livekit.yaml
├── docs/
│   └── wireguard-mesh.md       # Multi-machine architecture
└── README.md
```

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `LIVEKIT_URL` | `ws://localhost:7880` | LiveKit server WebSocket URL |
| `LIVEKIT_API_KEY` | — | LiveKit API key |
| `LIVEKIT_API_SECRET` | — | LiveKit API secret |
| `AGENT_NAME` | `Laira` | Agent name (set per process) |
| `ACP_ENABLED` | `true` | Use ACP bridge (vs legacy docker exec) |
| `ACP_LAIRA_URL` | `http://127.0.0.1:3133` | Hermes gateway URL |
| `ACP_LOKI_URL` | `http://172.20.0.3:8642` | OpenClaw shim URL |
| `ACP_STREAMING_AGENTS` | `laira,loki` | Agents with SSE streaming |
| `ACP_BUS_HOST` | `0.0.0.0` | Event Bus bind address |
| `ACP_BUS_PORT` | `9090` | Event Bus port |
| `EDGE_TTS_VOICE_LAIRA` | `de-DE-SeraphinaMultilingualNeural` | Laira's TTS voice |
| `EDGE_TTS_VOICE_LOKI` | `en-US-GuyNeural` | Loki's TTS voice |
| `WHISPER_MODEL` | `small` | faster-whisper model size |
| `LLM_BACKEND` | `anthropic` | LLM backend (anthropic/openai/ollama) |

## Validation

```bash
cd agent
source .venv/bin/activate
python -m pytest tests/ -v    # 57 tests
```

## License

Proprietary. Copyright (c) 2026 Giannis Zacharioudakis. All rights reserved.
