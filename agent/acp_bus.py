"""ACP Event Bus — lightweight WebSocket pub/sub broker for cross-session context.

All agent sessions (VoiceRoom, Telegram, Discord) connect to this bus.
Events flow through the bus in real-time.  Each topic maintains an in-memory
ring buffer of the last MAX_EVENTS events so new subscribers get a catchup.

Start:  python acp_bus.py
Default: ws://127.0.0.1:9090

Protocol (all messages are JSON):

  Client → Bus:
    {"action": "auth", "secret": "<shared-secret>"}
    {"action": "subscribe", "topics": ["room:skynet-comms", "agent:laira"]}
    {"action": "publish", "topic": "room:skynet-comms", "event": {<event>}}
    {"action": "recent", "topic": "room:skynet-comms", "n": 20}

  Bus → Client:
    {"type": "auth_ok"}
    {"type": "subscribed", "topics": [...]}
    {"type": "event", "topic": "...", "event": {<event>}}
    {"type": "recent", "topic": "...", "events": [{...}, ...]}
    {"type": "error", "message": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque

import websockets
from websockets.asyncio.server import serve, ServerConnection

logger = logging.getLogger("acp-bus")

HOST = os.getenv("ACP_BUS_HOST", "0.0.0.0")
PORT = int(os.getenv("ACP_BUS_PORT", "9090"))
SECRET = os.getenv("ACP_BUS_SECRET", "").strip()
MAX_EVENTS = int(os.getenv("ACP_BUS_MAX_EVENTS", "100"))

# topic → deque of events (ring buffer)
_buffers: dict[str, deque[dict]] = {}

# topic → set of connected clients
_subscribers: dict[str, set[ServerConnection]] = {}

# client → set of subscribed topics (for cleanup)
_client_topics: dict[ServerConnection, set[str]] = {}

# authenticated clients (if SECRET is set)
_authed: set[ServerConnection] = set()


def _get_buffer(topic: str) -> deque[dict]:
    if topic not in _buffers:
        _buffers[topic] = deque(maxlen=MAX_EVENTS)
    return _buffers[topic]


def _stamp_event(event: dict) -> dict:
    """Ensure event has a timestamp."""
    if "ts" not in event:
        event["ts"] = time.time()
    return event


async def _send_json(ws: ServerConnection, msg: dict) -> None:
    try:
        await ws.send(json.dumps(msg, ensure_ascii=False))
    except Exception:
        pass


async def _handle_auth(ws: ServerConnection, data: dict) -> None:
    if not SECRET:
        _authed.add(ws)
        await _send_json(ws, {"type": "auth_ok"})
        return
    if data.get("secret") == SECRET:
        _authed.add(ws)
        await _send_json(ws, {"type": "auth_ok"})
    else:
        await _send_json(ws, {"type": "error", "message": "auth_failed"})


async def _handle_subscribe(ws: ServerConnection, data: dict) -> None:
    topics = data.get("topics", [])
    if not isinstance(topics, list):
        await _send_json(ws, {"type": "error", "message": "topics must be a list"})
        return

    if ws not in _client_topics:
        _client_topics[ws] = set()

    for topic in topics:
        if not isinstance(topic, str) or len(topic) > 128:
            continue
        _client_topics[ws].add(topic)
        if topic not in _subscribers:
            _subscribers[topic] = set()
        _subscribers[topic].add(ws)

    await _send_json(ws, {"type": "subscribed", "topics": topics})


async def _handle_publish(ws: ServerConnection, data: dict) -> None:
    topic = data.get("topic", "")
    event = data.get("event")
    if not topic or not event or not isinstance(event, dict):
        await _send_json(ws, {"type": "error", "message": "need topic and event"})
        return

    event = _stamp_event(event)
    _get_buffer(topic).append(event)

    # Broadcast to all subscribers except the sender.
    # Supports prefix wildcards: subscribing to "room:*" matches any "room:..." topic.
    msg = json.dumps({"type": "event", "topic": topic, "event": event}, ensure_ascii=False)
    recipients: set[ServerConnection] = set()
    # Exact match
    recipients.update(_subscribers.get(topic, set()))
    # Wildcard match: "room:*" matches any "room:..." topic
    prefix = topic.split(":")[0] + ":*" if ":" in topic else ""
    if prefix:
        recipients.update(_subscribers.get(prefix, set()))
    dead: list[ServerConnection] = []
    for sub in recipients:
        if sub is ws:
            continue
        try:
            await sub.send(msg)
        except Exception:
            dead.append(sub)
    for d in dead:
        _remove_client(d)


async def _handle_recent(ws: ServerConnection, data: dict) -> None:
    topic = data.get("topic", "")
    n = min(int(data.get("n", 20)), MAX_EVENTS)
    buf = _get_buffer(topic)
    events = list(buf)[-n:]
    await _send_json(ws, {"type": "recent", "topic": topic, "events": events})


def _remove_client(ws: ServerConnection) -> None:
    topics = _client_topics.pop(ws, set())
    for topic in topics:
        subs = _subscribers.get(topic)
        if subs:
            subs.discard(ws)
            if not subs:
                del _subscribers[topic]
    _authed.discard(ws)


async def _handler(ws: ServerConnection) -> None:
    _client_topics[ws] = set()
    # If no secret, auto-auth
    if not SECRET:
        _authed.add(ws)
    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                await _send_json(ws, {"type": "error", "message": "invalid json"})
                continue

            action = data.get("action", "")

            if action == "auth":
                await _handle_auth(ws, data)
                continue

            # All other actions require auth
            if ws not in _authed:
                await _send_json(ws, {"type": "error", "message": "not authenticated"})
                continue

            if action == "subscribe":
                await _handle_subscribe(ws, data)
            elif action == "publish":
                await _handle_publish(ws, data)
            elif action == "recent":
                await _handle_recent(ws, data)
            else:
                await _send_json(ws, {"type": "error", "message": f"unknown action: {action}"})
    except websockets.ConnectionClosed:
        pass
    except Exception:
        logger.debug("Client handler error", exc_info=True)
    finally:
        _remove_client(ws)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    if not SECRET:
        logger.warning("ACP Event Bus running WITHOUT authentication. Set ACP_BUS_SECRET for production use.")
    logger.info("ACP Event Bus starting on ws://%s:%d (secret=%s)", HOST, PORT, "yes" if SECRET else "NO - OPEN")
    async with serve(_handler, HOST, PORT):
        await asyncio.get_running_loop().create_future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
