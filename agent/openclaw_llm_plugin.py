"""No-op LLM plugin for LiveKit AgentSession.

AgentSession requires an llm= parameter at construction, but we never actually
invoke the LLM pipeline — all responses go through session.say() after calling
OpenClaw directly. This provides a minimal LLM that satisfies the interface.
"""

import logging
import uuid

from livekit.agents import llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

logger = logging.getLogger("noop-llm")


class NoOpLLM(llm.LLM):
    """Minimal LLM that satisfies AgentSession but is never called."""

    def chat(self, *, chat_ctx: llm.ChatContext, tools=None, conn_options=DEFAULT_API_CONNECT_OPTIONS, **kwargs) -> "NoOpLLMStream":
        return NoOpLLMStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class NoOpLLMStream(llm.LLMStream):
    """Stream that emits nothing — should never be reached in normal operation."""

    def __init__(self, llm_instance: NoOpLLM, *, chat_ctx: llm.ChatContext, tools: list, conn_options):
        super().__init__(llm_instance, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._request_id = str(uuid.uuid4())

    async def _run(self) -> None:
        logger.warning("NoOpLLM._run() called — this should not happen in the simplified architecture")
        self._event_ch.send_nowait(
            llm.ChatChunk(
                id=self._request_id,
                delta=llm.ChoiceDelta(role="assistant", content=""),
            )
        )
