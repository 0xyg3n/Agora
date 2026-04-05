"""ACP Event Bus client — async WebSocket client for agents.

Connects to the ACP Event Bus, publishes events, subscribes to topics,
and delivers incoming events via an async callback.

Usage in agent.py:

    bus = AcpBusClient("ws://127.0.0.1:9090")
    await bus.connect()
    await bus.subscribe(["room:skynet-comms", "agent:laira"])
    bus.on_event = my_callback  # async def my_callback(topic, event): ...
    await bus.publish("room:skynet-comms", {"type": "voice_input", ...})
    recent = await bus.get_recent("room:skynet-comms", n=10)
    await bus.close()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Callable, Awaitable

import websockets
from websockets.asyncio.client import connect

logger = logging.getLogger("acp-bus-client")

EventCallback = Callable[[str, dict], Awaitable[None]]


class AcpBusClient:
    """Async WebSocket client for the ACP Event Bus."""

    def __init__(
        self,
        url: str | None = None,
        secret: str | None = None,
        reconnect_delay: float = 2.0,
    ):
        self.url = url or os.getenv("ACP_BUS_URL", "ws://127.0.0.1:9090")
        self.secret = secret or os.getenv("ACP_BUS_SECRET", "").strip()
        self.reconnect_delay = reconnect_delay
        self.on_event: EventCallback | None = None
        self._ws: Any = None
        self._subscriptions: list[str] = []
        self._recv_task: asyncio.Task | None = None
        self._running = False
        self._pending_responses: dict[str, asyncio.Future] = {}

    async def connect(self) -> bool:
        """Connect to the bus and authenticate. Returns True on success."""
        try:
            self._ws = await connect(self.url, close_timeout=5)
            # Authenticate
            await self._send({"action": "auth", "secret": self.secret})
            resp = await self._recv_one(timeout=5)
            if not resp or resp.get("type") != "auth_ok":
                logger.error("Bus auth failed: %s", resp)
                await self._ws.close()
                self._ws = None
                return False

            self._running = True
            self._recv_task = asyncio.create_task(self._recv_loop())
            logger.info("Connected to ACP bus at %s", self.url)
            return True
        except Exception as e:
            logger.warning("Failed to connect to ACP bus: %s", e)
            self._ws = None
            return False

    async def connect_with_retry(self, max_attempts: int = 5) -> bool:
        """Try connecting with retries. Returns True on success."""
        for attempt in range(max_attempts):
            if await self.connect():
                return True
            if attempt < max_attempts - 1:
                await asyncio.sleep(self.reconnect_delay)
        return False

    async def subscribe(self, topics: list[str]) -> None:
        """Subscribe to topics. Stores them for auto-resubscribe on reconnect."""
        self._subscriptions = list(set(self._subscriptions + topics))
        if self._ws:
            await self._send({"action": "subscribe", "topics": topics})

    async def publish(self, topic: str, event: dict) -> None:
        """Publish an event to a topic (fire-and-forget)."""
        if not self._ws:
            return
        event.setdefault("ts", time.time())
        try:
            await self._send({"action": "publish", "topic": topic, "event": event})
        except Exception:
            logger.debug("Publish failed", exc_info=True)

    async def get_recent(self, topic: str, n: int = 20) -> list[dict]:
        """Get the last N events for a topic. Blocks briefly for the response."""
        if not self._ws:
            return []
        req_key = f"recent:{topic}"
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_responses[req_key] = future
        try:
            await self._send({"action": "recent", "topic": topic, "n": n})
            resp = await asyncio.wait_for(future, timeout=5)
            return resp.get("events", [])
        except asyncio.TimeoutError:
            return []
        finally:
            self._pending_responses.pop(req_key, None)

    async def close(self) -> None:
        """Disconnect from the bus."""
        self._running = False
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recv_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._running

    # --- Internal ---

    async def _send(self, msg: dict) -> None:
        if self._ws:
            await self._ws.send(json.dumps(msg, ensure_ascii=False))

    async def _recv_one(self, timeout: float = 5) -> dict | None:
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
            return json.loads(raw)
        except Exception:
            return None

    async def _recv_loop(self) -> None:
        """Background task: receive messages and dispatch events."""
        while self._running and self._ws:
            try:
                raw = await self._ws.recv()
                data = json.loads(raw)
            except websockets.ConnectionClosed:
                logger.warning("Bus connection closed, attempting reconnect")
                await self._reconnect()
                continue
            except Exception:
                if self._running:
                    logger.debug("Bus recv error", exc_info=True)
                continue

            msg_type = data.get("type", "")

            if msg_type == "event":
                topic = data.get("topic", "")
                event = data.get("event", {})
                if self.on_event:
                    try:
                        await self.on_event(topic, event)
                    except Exception:
                        logger.debug("Event callback error", exc_info=True)

            elif msg_type == "recent":
                topic = data.get("topic", "")
                key = f"recent:{topic}"
                fut = self._pending_responses.get(key)
                if fut and not fut.done():
                    fut.set_result(data)

            elif msg_type == "error":
                logger.warning("Bus error: %s", data.get("message"))

    async def _reconnect(self) -> None:
        """Attempt to reconnect and resubscribe."""
        self._ws = None
        for attempt in range(10):
            if not self._running:
                return
            await asyncio.sleep(self.reconnect_delay)
            try:
                self._ws = await connect(self.url, close_timeout=5)
                await self._send({"action": "auth", "secret": self.secret})
                resp = await self._recv_one(timeout=5)
                if resp and resp.get("type") == "auth_ok":
                    if self._subscriptions:
                        await self._send({"action": "subscribe", "topics": self._subscriptions})
                    logger.info("Reconnected to ACP bus (attempt %d)", attempt + 1)
                    return
                await self._ws.close()
                self._ws = None
            except Exception:
                self._ws = None
                continue
        logger.error("Failed to reconnect to ACP bus after 10 attempts")
        self._running = False
