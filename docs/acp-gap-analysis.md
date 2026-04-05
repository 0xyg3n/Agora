# ACP Gap Analysis — Agora

## Context

The Agora project recently replaced its `docker exec`-based bridge with an ACP (Agent Communication Protocol) layer. Laira connects to the Hermes gateway via streaming SSE on port 3133. Loki connects to an OpenClaw API shim via non-streaming HTTP on 172.20.0.3:8642. A cross-session context sync writes events to the ACP Event Bus.

This document catalogs every gap found across security, efficiency, quality, and architecture — with severity, concrete fix, effort, and priority.

---

## 1. SECURITY GAPS

### S1. No authentication on OpenClaw API shim — CRITICAL
**File:** `agent/openclaw_api_shim.py:108-191`  
**Issue:** The shim listens on `0.0.0.0:8642` inside the Loki container with zero authentication. Any container on the Docker network can send chat completions requests, triggering expensive LLM calls.  
**Fix:** Add API key validation — check `Authorization: Bearer <key>` header against an env var (`SHIM_API_KEY`). Return 401 for missing/wrong keys.  
**Effort:** Quick fix  
**Priority:** 1

### S2. Stderr leaked in shim error responses — CRITICAL
**File:** `agent/openclaw_api_shim.py:45-46`  
**Issue:** `openclaw agent` stderr (may contain stack traces, paths, env vars) is returned verbatim in the HTTP response: `{"error": "openclaw agent failed: <stderr>"}`.  
**Fix:** Log full stderr server-side; return generic `{"error": {"message": "Internal server error"}}` to clients.  
**Effort:** Quick fix  
**Priority:** 2

### S3. No request body size limit in shim — HIGH
**File:** `agent/openclaw_api_shim.py:83-85`  
**Issue:** Content-Length is parsed but not bounded. A client sending `Content-Length: 2147483647` will attempt to allocate ~2 GB of memory.  
**Fix:** Add `MAX_BODY_SIZE = 1_000_000` check before `readexactly()`. Return 413 if exceeded.  
**Effort:** Quick fix  
**Priority:** 3

### S4. Path traversal via room_name in context cache — HIGH
**File:** `agent/agent.py:185`  
**Issue:** `_CONTEXT_CACHE_PATH = f"/srv/project/.cache/{room_name}-{agent_name}-context.jsonl"` — a room named `../../etc/cron.d/evil` would write outside the intended directory.  
**Fix:** Sanitize room_name: `room_name = re.sub(r'[^a-zA-Z0-9_-]', '', ctx.room.name)[:64]`  
**Effort:** Quick fix  
**Priority:** 4

### S5. Session IDs are predictable — MEDIUM
**File:** `agent/agent.py:282`  
**Issue:** `_session_id = f"livekit-{room_name}"` — anyone who knows the room name can predict the session ID and potentially inject messages into the same Hermes/OpenClaw session from another endpoint.  
**Fix:** Append a per-process random suffix: `f"livekit-{room_name}-{uuid4().hex[:8]}"`. Or accept the risk since access to the gateway requires network access to the Docker host.  
**Effort:** Quick fix  
**Priority:** 8

### S6. No session_id format validation in ACP bridge — MEDIUM
**File:** `agent/acp_bridge.py:96-103`  
**Issue:** Session ID is placed directly in HTTP headers (`X-Hermes-Session-Id`). A newline in the session ID enables HTTP header injection.  
**Fix:** Validate format with regex: `re.match(r'^[a-zA-Z0-9_-]{1,128}$', session_id)`. Reject or sanitize otherwise.  
**Effort:** Quick fix  
**Priority:** 9

### S7. Participant name injection in chat echo and context log — MEDIUM
**File:** `agent/agent.py:790-798`, `acp_context_sync.py:50`  
**Issue:** `sender` (from `p.name` or `p.identity`) is stored in JSON and echoed to chat unsanitized. A name containing `"` or newlines corrupts the JSONL log or could enable XSS if the frontend renders it as HTML.  
**Fix:** Sanitize sender: strip control chars, limit to 64 chars alphanumeric + spaces. `json.dumps` handles JSON escaping, but the JSONL log corruption risk is real if sender contains `\n`.  
**Effort:** Quick fix  
**Priority:** 10

### S8. User input echoed in participant attribute status text — LOW
**File:** `agent/agent.py:373`  
**Issue:** `status=f"Calling agent about: {human_text}"` — user input visible in LiveKit participant attributes, which may be logged by the LiveKit server or exposed to the frontend.  
**Fix:** Use generic status: `"Processing voice input"`.  
**Effort:** Quick fix  
**Priority:** 14

### S9. No TLS for ACP HTTP calls — LOW (localhost-only)
**File:** `agent/acp_bridge.py:47-48`  
**Issue:** ACP URLs are HTTP, not HTTPS. Acceptable for localhost/Docker bridge, but if someone reconfigures `ACP_*_URL` to point at a remote host, API keys travel in cleartext.  
**Fix:** Log a warning if URL is non-localhost and non-HTTPS.  
**Effort:** Quick fix  
**Priority:** 15

---

## 2. EFFICIENCY GAPS

### E1. Sentence-split regex breaks on abbreviations — HIGH
**File:** `agent/agent.py:404`  
**Issue:** `r'(?<=[.!?])\s+'` splits on any period + space, so `"Dr. Smith"` → `["Dr.", "Smith"]`, `"U.S. history"` → `["U.S.", "history"]`. This doubles TTS calls and creates unnatural pauses.  
**Fix:** Add abbreviation guard — either a negative lookbehind for common abbreviations or a more nuanced split that requires an uppercase letter after the period: `r'(?<=[.!?])\s+(?=[A-Z])'` (still imperfect but much better). Best: split only on `[.!?]` followed by `\s+` AND preceded by a word of 4+ chars.  
**Effort:** Medium (needs testing with real voice output)  
**Priority:** 5

### E2. session.say() blocks per sentence in streaming — HIGH
**File:** `agent/agent.py:417`  
**Issue:** `await session.say(cleaned)` waits for TTS to complete before the next sentence streams in. For a 3-sentence response, the user hears nothing until sentence 1 finishes rendering, then waits again for sentence 2.  
**Fix:** Queue sentences into a background TTS task instead of awaiting inline. Use `asyncio.create_task(session.say(cleaned))` or a bounded asyncio.Queue consumed by a TTS worker coroutine.  
**Effort:** Medium (must ensure ordering and handle interrupts)  
**Priority:** 6

### E3. OpenClaw shim spawns subprocess per request — MEDIUM
**File:** `agent/openclaw_api_shim.py:28-42`  
**Issue:** Each HTTP request spawns `openclaw agent` as a subprocess (~50-200ms overhead). This is better than docker exec (~300-500ms) but still adds latency.  
**Fix:** Long-term: OpenClaw should expose a native HTTP API. Short-term: keep a pool of pre-warmed subprocesses or use `openclaw agent --local` with a persistent Node.js process. Alternatively, connect directly to the OpenClaw WebSocket gateway from Python using `websockets` library.  
**Effort:** Large  
**Priority:** 11

### E4. Context sync does synchronous file I/O in async context — MEDIUM
**File:** `agent/acp_context_sync.py:56-62`  
**Issue:** `publish_event()` calls `open()` and `write()` synchronously. On a busy filesystem this blocks the event loop by 1-50ms.  
**Fix:** Wrap in `asyncio.to_thread()` or use a write-behind buffer (accumulate events in memory, flush periodically).  
**Effort:** Quick fix  
**Priority:** 12

### E5. Context sync _maybe_prune() reads entire file on every write — MEDIUM
**File:** `agent/acp_context_sync.py:65-73`  
**Issue:** Every `publish_event()` call reads the entire JSONL file to count lines. At 200 lines (~100KB), this adds ~5ms per write.  
**Fix:** Track line count in memory. Only read the file on startup to get initial count.  
**Effort:** Quick fix  
**Priority:** 13

### E6. No aiohttp TCPConnector tuning — LOW
**File:** `agent/acp_bridge.py:83`  
**Issue:** Default aiohttp connector settings. Not a real problem at current scale (2 agents, 1 connection each), but would matter with 5+ agents.  
**Fix:** Add explicit `TCPConnector(limit_per_host=10, ttl_dns_cache=300, keepalive_timeout=30)`.  
**Effort:** Quick fix  
**Priority:** 16

---

## 3. QUALITY GAPS

### Q1. Zero test coverage for ACP modules — HIGH
**File:** `agent/tests/` (only `test_openclaw_bridge.py` and `test_runtime_utils.py` exist)  
**Issue:** `acp_bridge.py` (14KB), `acp_protocol.py`, `acp_context_sync.py`, `openclaw_api_shim.py` have NO tests. The sentence-split regex bug (E1) would have been caught by even basic tests.  
**Fix:** Add `test_acp_bridge.py` (SSE parsing, non-streaming fallback, error handling, health check), `test_acp_protocol.py` (serialization), `test_acp_context_sync.py` (publish/read/prune), `test_sentence_split.py` (abbreviations, edge cases).  
**Effort:** Medium  
**Priority:** 7

### Q2. No retry logic for failed ACP calls — MEDIUM
**File:** `agent/acp_bridge.py:217-352`  
**Issue:** A single transient failure (connection reset, 503 gateway restart) returns an error immediately. No retries.  
**Fix:** Add 1 retry with 500ms delay for connection errors and 5xx HTTP statuses. Don't retry 4xx or timeouts (those are deliberate).  
**Effort:** Quick fix  
**Priority:** 17

### Q3. No graceful mid-conversation fallback from ACP to legacy — MEDIUM
**File:** `agent/agent.py:38`  
**Issue:** `_ACP_ENABLED` is set once at process start. If the Hermes API server goes down mid-conversation, all subsequent calls fail with "gateway unreachable" instead of falling back to docker exec.  
**Fix:** In `_ask_agent()`, catch ACP failures and fall back to `_ask_openclaw_legacy()` with a log warning. Could add a circuit breaker: after N consecutive failures, disable ACP for M seconds.  
**Effort:** Medium  
**Priority:** 18

### Q4. Forkserver zombie processes not cleaned up — MEDIUM
**File:** LiveKit agents framework (external)  
**Issue:** When agents are killed with `pkill`, their forked child processes (from `multiprocessing.forkserver`) survive as orphans consuming 100-500MB each. The `start-multi-agents.sh` script addresses this, but manual starts don't.  
**Fix:** Add cleanup to the agent shutdown path: `pkill -f "forkserver.*livekit"` or use `prctl(PR_SET_PDEATHSIG)` to auto-kill children. Alternatively, always use `start-multi-agents.sh`.  
**Effort:** Quick fix  
**Priority:** 19

### Q5. Observability event failures are silent — LOW
**File:** `agent/acp_bridge.py:144-145`  
**Issue:** Failed observability posts logged at DEBUG level (invisible in normal operation).  
**Fix:** Log at WARNING for repeated failures. Add a counter; if >5 failures in 60s, log once at WARNING.  
**Effort:** Quick fix  
**Priority:** 20

---

## 4. ARCHITECTURE GAPS

### A1. "Protocol" is just a data schema — no real protocol semantics — MEDIUM
**File:** `agent/acp_protocol.py`  
**Issue:** `MessageType` enum has 6 values but only `VOICE_INPUT` is ever used. `ACPMessage.to_chat_messages()` just extracts the content field. There's no message routing, no handshake, no versioning, no acknowledgment — it's a data container, not a protocol.  
**Fix:** Either rename to `acp_types.py` (honest naming) or implement actual protocol semantics: message routing based on `MessageType`, version negotiation, delivery acknowledgment.  
**Effort:** Medium (rename) / Large (real protocol)  
**Priority:** 21

### A2. Agent registry is hardcoded to 2 agents — HIGH
**File:** `agent/acp_bridge.py:46-49`, `agent/agent.py:89`, `agent/openclaw_bridge.py:38-40`  
**Issue:** Adding a 3rd agent requires edits in 3 files + env vars. Agent names, URLs, voice defaults, and streaming capabilities are all scattered across different dicts.  
**Fix:** Consolidate into a single agent config registry (e.g. `agent/agent_registry.py` or a `agents.yaml` config file) that maps agent name → URL, voice, streaming, container name. All code reads from this registry.  
**Effort:** Medium  
**Priority:** 22

### A3. Cross-session context sync is write-only — nobody reads it — MEDIUM
**File:** `agent/acp_context_sync.py`  
**Issue:** Events are published to `/tmp/virtualcomms-context.jsonl` but `read_recent()` is never called anywhere in the codebase. No Telegram/Discord adapter consumes these events. The file is on `/tmp` (non-persistent, single-host).  
**Fix:** Either (a) wire `read_recent()` into the Hermes/OpenClaw system prompts so agents gain cross-session awareness, or (b) remove the feature until consumers exist.  
**Effort:** Medium (option a) / Quick (option b)  
**Priority:** 23

### A4. OpenClaw shim is a fragile temporary hack — MEDIUM
**File:** `agent/openclaw_api_shim.py`  
**Issue:** Hand-rolled HTTP parser, no auth, subprocess per request, must be manually started inside the container, not in the container's entrypoint. If the container restarts, the shim is gone.  
**Fix:** Short-term: add shim startup to Loki's `entrypoint.sh`. Long-term: lobby for native OpenClaw HTTP API or build a WebSocket-to-HTTP bridge using the `websockets` library.  
**Effort:** Quick fix (entrypoint) / Large (WebSocket bridge)  
**Priority:** 24

### A5. No abstract bridge interface — both bridges have incompatible APIs — LOW
**File:** `agent/acp_bridge.py` vs `agent/openclaw_bridge.py`  
**Issue:** `send_to_openclaw(agent_name, message, ...) → dict` vs `stream_from_gateway(agent_name, ACPMessage, ...) → AsyncIterator`. No shared interface means `_ask_agent()` must know both APIs intimately.  
**Fix:** Define an abstract `AgentBridge` protocol with `async def send(agent_name, message, session_id) → str` and `async def stream(agent_name, message, session_id) → AsyncIterator[str]`. Both bridges implement it.  
**Effort:** Medium  
**Priority:** 25

---

## Priority Implementation Order

| # | ID | Severity | Fix | Effort |
|---|-----|----------|-----|--------|
| 1 | S1 | CRITICAL | Auth on OpenClaw shim | Quick |
| 2 | S2 | CRITICAL | Scrub stderr from error responses | Quick |
| 3 | S3 | HIGH | Body size limit in shim | Quick |
| 4 | S4 | HIGH | Sanitize room_name for path traversal | Quick |
| 5 | E1 | HIGH | Fix sentence-split regex | Medium |
| 6 | E2 | HIGH | Non-blocking TTS queue for streaming | Medium |
| 7 | Q1 | HIGH | Add ACP test suite | Medium |
| 8 | S5 | MEDIUM | Add randomness to session IDs | Quick |
| 9 | S6 | MEDIUM | Validate session_id format | Quick |
| 10 | S7 | MEDIUM | Sanitize participant names | Quick |
| 11 | E3 | MEDIUM | Optimize shim subprocess overhead | Large |
| 12 | E4 | MEDIUM | Async file I/O in context sync | Quick |
| 13 | E5 | MEDIUM | In-memory line count for prune | Quick |
| 14 | S8 | LOW | Generic status text (no user input) | Quick |
| 15 | S9 | LOW | Warn on non-HTTPS remote URLs | Quick |
| 16 | E6 | LOW | TCPConnector tuning | Quick |
| 17 | Q2 | MEDIUM | ACP retry logic (1 retry) | Quick |
| 18 | Q3 | MEDIUM | Mid-conversation ACP→legacy fallback | Medium |
| 19 | Q4 | MEDIUM | Forkserver cleanup on shutdown | Quick |
| 20 | Q5 | LOW | Warn on repeated observability failures | Quick |
| 21 | A1 | MEDIUM | Rename or implement real protocol | Medium |
| 22 | A2 | HIGH | Centralized agent registry | Medium |
| 23 | A3 | MEDIUM | Wire context sync to consumers or remove | Medium |
| 24 | A4 | MEDIUM | Add shim to entrypoint.sh | Quick |
| 25 | A5 | LOW | Abstract bridge interface | Medium |

**Quick wins (items 1-4, 8-10, 12-16, 19-20):** 15 fixes, each under 30 minutes — can be done in one session.  
**Medium effort (items 5-7, 17-18, 21-25):** Require design thought and testing.  
**Large effort (item 11):** Architectural change to OpenClaw integration.
