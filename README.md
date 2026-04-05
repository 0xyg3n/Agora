<p align="center">
  <h1 align="center">Agora</h1>
  <p align="center">Real-time voice rooms where humans and AI agents collaborate across platforms</p>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/node-20+-green?logo=node.js&logoColor=white" alt="Node.js 20+">
  <img src="https://img.shields.io/badge/LiveKit-1.5+-purple?logo=webrtc&logoColor=white" alt="LiveKit">
  <img src="https://img.shields.io/badge/license-Proprietary-red" alt="License">
  <img src="https://img.shields.io/badge/tests-57%20passing-brightgreen" alt="Tests">
</p>

---

Agora is an open platform for real-time voice collaboration between humans and AI agents. Agents join voice rooms as participants — they hear you, speak back, and collaborate with each other. Cross-session awareness via the ACP Event Bus means agents know what's happening across all their connected platforms (voice rooms, Telegram, Discord).

## Screenshots

| Pre-join | In-call |
|----------|---------|
| ![Pre-join](docs/screenshots/pre-join.png) | ![In-call](docs/screenshots/in-call-overview.png) |

## What Is Agora?

- **Voice rooms with AI agents**: Humans and agents share a LiveKit room. Agents hear speech, respond via TTS, and see each other's messages.
- **Any LLM backend**: Works with Hermes Agent, OpenClaw, or any platform that exposes an HTTP API. Adding a new agent is config, not code.
- **Local voice pipeline**: Silero VAD, faster-whisper STT, edge-tts — no cloud voice APIs, no per-minute charges.
- **Cross-session awareness**: The ACP Event Bus connects voice rooms, Telegram, and Discord into a shared context layer. An agent on Telegram can answer "what happened in the voice room?" by querying the bus.
- **Progressive TTS**: Agents speak the first sentence while still generating the rest. No waiting for the full response.

## Architecture

```
                         ┌──────────────────────────────┐
                         │        Browser UI             │
                         │  Pre-join  Voice Room  Chat   │
                         └─────────────┬────────────────┘
                                       │ WebRTC
                         ┌─────────────▼────────────────┐
                         │     LiveKit Media Server      │
                         │  Humans + Agents in one room  │
                         └──────┬───────────────┬───────┘
                                │               │
                  ┌─────────────▼──┐    ┌───────▼─────────────┐
                  │  Agent: Laira  │    │  Agent: Loki        │
                  │  (Hermes)      │    │  (OpenClaw)         │
                  │                │    │                     │
                  │  Silero VAD    │    │  Silero VAD         │
                  │  Whisper STT   │    │  Whisper STT        │
                  │  edge-tts      │    │  edge-tts           │
                  │       │        │    │       │             │
                  │  ACP Bridge    │    │  ACP Bridge         │
                  │  (SSE stream)  │    │  (SSE + shim)       │
                  └───────┬────────┘    └───────┬─────────────┘
                          │                     │
                          │  ┌───────────────┐  │
                          └──► ACP Event Bus ◄──┘
                             │  (WebSocket)  │
                             │  Cross-session │
                             │  pub/sub       │
                             └──────┬────────┘
                                    │
                     ┌──────────────▼──────────────┐
                     │       Agent Gateways         │
                     │  Hermes ─── HTTP streaming   │
                     │  OpenClaw ── API shim + SSE  │
                     │  Custom ─── any HTTP agent   │
                     └──────────────────────────────┘
```

## Supported Agent Platforms

### Hermes Agent (native support)

- Direct HTTP streaming via the Hermes API server
- SSE streaming for progressive TTS — agent speaks while still thinking
- Native `acp_bus_query` tool registered in the Hermes tool system
- Agora registered as a first-class platform (`Platform.AGORA`)
- Session persistence via `X-Hermes-Session-Id` header
- Full access to Hermes memory, skills, and tools

### OpenClaw (supported via API shim)

- OpenAI-compatible HTTP wrapper deployed inside the container (`openclaw_api_shim.py`)
- SSE streaming — response split into sentences, streamed as chunks
- Cross-session bus query via workspace skill (`acp-bus-query`)
- Agora awareness in workspace configuration
- Session persistence via session ID routing

### Any HTTP Agent (bring your own)

Any agent that exposes an OpenAI-compatible `/v1/chat/completions` endpoint works out of the box. Add it to `agent/agent_registry.py`:

```python
AgentConfig(
    name="MyAgent",
    container="skynet-myagent",
    acp_url="http://172.20.0.X:8642",
    voice="en-US-AriaNeural",
    streaming=True,
)
```

No code changes needed — just config and restart.

## ACP Event Bus

The ACP Event Bus (`agent/acp_bus.py`) is a lightweight WebSocket pub/sub broker that provides cross-session awareness without duplicating data into each agent's backend.

**How it works:**

1. Agents publish events when things happen (voice input, responses, actions)
2. Any session on any platform queries the bus via the `acp_bus_query` tool
3. The bus holds a ring buffer of the last 100 events per topic — in-memory, no disk

**Topics:** `room:agora-comms`, `agent:laira`, `agent:loki`

**Event format:**
```json
{
  "type": "voice_input",
  "speaker": "Giannis",
  "agent": "laira",
  "content": "Hey everyone, can you hear me?",
  "ts": 1712345678.123
}
```

**Protocol:** JSON over WebSocket — `auth`, `subscribe`, `publish`, `recent`

### Cross-Session Flow

```
Agora Voice Room                    Telegram
     │                                  │
     │  Giannis speaks                  │
     │  "hello everyone"                │
     │         │                        │
     │    publish to bus                │
     │         │                        │
     │    ┌────▼────┐                   │
     │    │ ACP Bus │                   │
     │    └────┬────┘                   │
     │         │                        │
     │         │         Giannis asks:  │
     │         │    "what happened in   │
     │         │     the voice room?"   │
     │         │              │         │
     │         │     acp_bus_query()    │
     │         │              │         │
     │         └──────────────┘         │
     │                                  │
     │              Agent responds:     │
     │         "Giannis said hello      │
     │          in the voice room"      │
```

## Voice Pipeline

| Stage | Technology | Notes |
|-------|-----------|-------|
| Voice Activity Detection | Silero VAD | Local, no cloud API |
| Speech-to-Text | faster-whisper | Local, configurable model (`small`/`medium`/`large`) |
| Text-to-Speech | edge-tts | Free Microsoft TTS, per-agent voice selection |
| Progressive TTS | Sentence streaming | First sentence plays while rest generates |

## WireGuard Mesh (Multi-Machine)

Agora can scale across multiple machines using WireGuard as the network layer. The ACP bus listens on the WireGuard interface, and any machine on the mesh can connect agents to it. GPU-heavy workloads (TTS, STT, local LLMs) run on machines with GPUs while the bus and room stay on the VPS.

See [docs/wireguard-mesh.md](docs/wireguard-mesh.md) for the full architecture.

## Quick Start

### Prerequisites

- Docker with agent containers (`skynet-laira`, `skynet-loki`)
- Python 3.12+ with venv at `agent/.venv`
- Node.js 20+ for the frontend
- LiveKit server

### 1. Clone and configure

```bash
git clone https://github.com/0xyg3n/Agora.git
cd Agora/VirtualComms_main
cp .env.example .env
# Edit .env with your LiveKit keys and agent URLs
```

### 2. Start LiveKit

```bash
cd server
docker compose up -d
```

### 3. Start the frontend

```bash
cd frontend
npm install
npm run build
npx tsx server.ts
```

Frontend runs at `http://127.0.0.1:3210`.

### 4. Start agents + ACP bus

```bash
./scripts/start-multi-agents.sh
```

This starts the ACP Event Bus, then both agents. Or start manually:

```bash
cd agent
source .venv/bin/activate
python acp_bus.py &                          # Event bus
AGENT_NAME=Laira ACP_ENABLED=true python agent.py dev &   # Laira
AGENT_NAME=Loki  ACP_ENABLED=true python agent.py dev &   # Loki
```

### 5. Open the app

```
http://127.0.0.1:3210
```

Remote access via SSH tunnel:

```bash
ssh -L 3210:127.0.0.1:3210 -L 7880:127.0.0.1:7880 <host>
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LIVEKIT_URL` | `ws://localhost:7880` | LiveKit WebSocket URL |
| `LIVEKIT_API_KEY` | — | LiveKit API key |
| `LIVEKIT_API_SECRET` | — | LiveKit API secret |
| `AGENT_NAME` | `Laira` | Agent name (set per process) |
| `ACP_ENABLED` | `true` | Use ACP bridge vs legacy docker exec |
| `ACP_LAIRA_URL` | `http://127.0.0.1:3133` | Hermes gateway URL |
| `ACP_LOKI_URL` | `http://172.20.0.3:8642` | OpenClaw shim URL |
| `ACP_STREAMING_AGENTS` | `laira,loki` | Agents with SSE streaming support |
| `ACP_BUS_HOST` | `0.0.0.0` | Event Bus bind address |
| `ACP_BUS_PORT` | `9090` | Event Bus port |
| `ACP_BUS_SECRET` | _(empty)_ | Bus auth secret (optional) |
| `EDGE_TTS_VOICE_LAIRA` | `de-DE-SeraphinaMultilingualNeural` | Laira's TTS voice |
| `EDGE_TTS_VOICE_LOKI` | `en-US-GuyNeural` | Loki's TTS voice |
| `WHISPER_MODEL` | `small` | faster-whisper model size |
| `LLM_BACKEND` | `anthropic` | LLM backend (`anthropic`/`openai`/`ollama`) |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-20250514` | Anthropic model ID |

## Adding a New Agent

1. Deploy your agent gateway (any OpenAI-compatible HTTP endpoint)
2. Add to `agent/agent_registry.py`:

```python
AgentConfig(
    name="Nova",
    container="skynet-nova",
    acp_url="http://172.20.0.5:8642",
    voice="en-US-JennyNeural",
    streaming=True,
)
```

3. Set the voice: `EDGE_TTS_VOICE_NOVA=en-US-JennyNeural` in `.env`
4. Start: `AGENT_NAME=Nova ACP_ENABLED=true python agent.py dev`

## Repository Layout

```
agora/
├── agent/
│   ├── agent.py                # Main voice agent
│   ├── acp_bridge.py           # HTTP streaming bridge to gateways
│   ├── acp_bus.py              # ACP Event Bus server
│   ├── acp_bus_client.py       # Bus client library
│   ├── acp_protocol.py         # Message types
│   ├── agent_registry.py       # Agent config registry
│   ├── openclaw_api_shim.py    # OpenClaw HTTP/SSE shim
│   ├── edge_tts_plugin.py      # TTS plugin
│   ├── whisper_stt_plugin.py   # STT plugin
│   ├── vision.py               # Vision/camera module
│   ├── runtime_utils.py        # Helpers
│   └── tests/                  # 57 tests
├── frontend/
│   ├── server.ts               # Token server + ops API
│   ├── src/                    # React UI
│   └── terminal/               # PTY terminal backend
├── scripts/
│   ├── start-multi-agents.sh   # Start everything
│   ├── deploy.sh               # Deployment script
│   └── check-openclaw-compat.sh
├── server/
│   ├── docker-compose.yml      # LiveKit server
│   └── livekit.yaml
├── config/
│   └── openclaw-version.txt
├── docs/
│   ├── acp-gap-analysis.md     # Security/quality audit
│   ├── wireguard-mesh.md       # Multi-machine architecture
│   └── screenshots/
└── README.md
```

## Security

- **Authentication**: API key validation on the OpenClaw shim, bus auth secret support
- **Input sanitization**: Room names, session IDs, and participant names are sanitized against path traversal and header injection
- **Request limits**: 1MB body size limit on the API shim
- **Error scrubbing**: Internal errors never leak stack traces to clients
- **Session isolation**: Per-session IDs with random suffixes prevent session hijacking
- **TLS warning**: ACP bridge warns if non-HTTPS URLs are used for non-local endpoints

See [docs/acp-gap-analysis.md](docs/acp-gap-analysis.md) for the full security audit.

## Tests

```bash
cd agent
source .venv/bin/activate
python -m pytest tests/ -v
```

```
57 passed in 0.8s
```

Coverage: ACP protocol, event bus, agent registry, sentence splitting, bridge helpers, runtime utilities.

## License

Proprietary. Copyright (c) 2026 Giannis Zacharioudakis. All rights reserved.
