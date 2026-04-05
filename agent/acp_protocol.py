"""ACP message types — data structures for the ACP bridge layer.

Defines the structured message types exchanged between the LiveKit voice
layer and the Hermes Agent gateway over HTTP.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MessageType(str, Enum):
    """Types of messages in the ACP protocol."""
    VOICE_INPUT = "voice_input"
    TEXT_INPUT = "text_input"
    AGENT_RESPONSE = "agent_response"
    AGENT_TO_AGENT = "agent_to_agent"
    ACTION = "action"
    ERROR = "error"


class ChunkType(str, Enum):
    """Types of streaming response chunks."""
    TEXT_CHUNK = "text_chunk"
    ACTION = "action"
    DONE = "done"
    ERROR = "error"


@dataclass
class ACPMessage:
    """A message sent to the Hermes gateway."""
    type: MessageType
    session_id: str
    sender: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_chat_messages(self, *, system_prompt: str | None = None) -> list[dict[str, str]]:
        """Convert to OpenAI-compatible chat messages list."""
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": self.content})
        return messages


@dataclass
class ACPResponseChunk:
    """A chunk from a streaming gateway response."""
    type: ChunkType
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
