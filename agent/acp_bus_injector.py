"""ACP Bus → Gateway context injector.

Subscribes to the ACP Event Bus and periodically injects room activity
summaries into the Hermes and OpenClaw gateway sessions.  This gives
Telegram/Discord sessions awareness of what happened in the voice room.

Start:  python acp_bus_injector.py
Runs alongside the ACP bus and agents on the host.
"""

import asyncio
import json
import logging
import os
import time
from collections import deque

import aiohttp
from acp_bus_client import AcpBusClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("bus-injector")

BUS_URL = os.getenv("ACP_BUS_URL", "ws://127.0.0.1:9090")
LAIRA_URL = os.getenv("ACP_LAIRA_URL", "http://127.0.0.1:3133")
LOKI_URL = os.getenv("ACP_LOKI_URL", "http://172.20.0.3:8642")
INJECT_INTERVAL = int(os.getenv("ACP_INJECT_INTERVAL", "30"))  # seconds

# Recent events buffer (shared across all topics)
_recent: deque[dict] = deque(maxlen=20)


async def _inject_context(session: aiohttp.ClientSession, url: str, name: str) -> None:
    """Send a context summary to a gateway session."""
    if not _recent:
        return
    lines = []
    for evt in list(_recent)[-8:]:
        spk = evt.get("speaker", "?")
        ct = evt.get("content", "")[:120]
        etype = evt.get("type", "")
        ago = int(time.time() - evt.get("ts", time.time()))
        label = "said" if etype == "voice_input" else "responded"
        lines.append(f"  {spk} {label} ({ago}s ago): {ct}")
    summary = "[LiveKit VoiceRoom activity]\n" + "\n".join(lines)

    body = {
        "model": "hermes-agent",
        "messages": [
            {"role": "system", "content": summary},
            {"role": "user", "content": "[context sync — no reply needed]"},
        ],
    }
    try:
        async with session.post(
            f"{url}/v1/chat/completions",
            json=body,
            headers={"X-Hermes-Session-Id": f"livekit-context-sync-{name}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                logger.debug("Injected context into %s", name)
            else:
                logger.debug("Inject to %s returned %d", name, resp.status)
    except Exception as e:
        logger.debug("Inject to %s failed: %s", name, e)


async def main() -> None:
    bus = AcpBusClient(url=BUS_URL)
    ok = await bus.connect_with_retry(max_attempts=10)
    if not ok:
        logger.error("Could not connect to ACP bus at %s", BUS_URL)
        return

    await bus.subscribe(["room:*"])
    logger.info("Connected to bus, subscribing to all room topics")

    # Also subscribe to specific known rooms
    await bus.subscribe(["room:skynet-comms"])

    async def on_event(topic: str, event: dict) -> None:
        _recent.append(event)

    bus.on_event = on_event

    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(INJECT_INTERVAL)
            if _recent:
                await _inject_context(session, LAIRA_URL, "laira")
                await _inject_context(session, LOKI_URL, "loki")
                logger.info("Injected %d events into gateway sessions", len(_recent))


if __name__ == "__main__":
    asyncio.run(main())
