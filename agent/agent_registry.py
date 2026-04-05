"""Centralized agent configuration registry.

All agent-specific config (URL, voice, container, streaming) is defined
here.  Adding a new agent only requires adding an entry to AGENTS — no
code changes in agent.py, acp_bridge.py, or openclaw_bridge.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentConfig:
    """Configuration for a single agent."""
    name: str
    container: str
    acp_url: str
    voice: str
    streaming: bool = True

    @property
    def name_lower(self) -> str:
        return self.name.lower()


def _load_agents() -> dict[str, AgentConfig]:
    """Load agent configs from defaults + env var overrides."""
    defaults = [
        AgentConfig(
            name="Laira",
            container="skynet-laira",
            acp_url=os.getenv("ACP_LAIRA_URL", "http://127.0.0.1:3133"),
            voice=os.getenv("EDGE_TTS_VOICE_LAIRA", "de-DE-SeraphinaMultilingualNeural"),
            streaming=True,
        ),
        AgentConfig(
            name="Loki",
            container="skynet-loki",
            acp_url=os.getenv("ACP_LOKI_URL", "http://172.20.0.3:8642"),
            voice=os.getenv("EDGE_TTS_VOICE_LOKI", "en-US-GuyNeural"),
            streaming=True,
        ),
    ]
    # Support additional agents via ACP_EXTRA_AGENTS=name1:url1:voice1,name2:url2:voice2
    extra = os.getenv("ACP_EXTRA_AGENTS", "").strip()
    if extra:
        for entry in extra.split(","):
            parts = entry.strip().split(":")
            if len(parts) >= 3:
                defaults.append(AgentConfig(
                    name=parts[0],
                    container=f"skynet-{parts[0].lower()}",
                    acp_url=parts[1] + ":" + parts[2],  # rejoin url parts
                    voice=parts[3] if len(parts) > 3 else "en-US-AriaNeural",
                    streaming=parts[4].lower() == "true" if len(parts) > 4 else False,
                ))
    return {a.name_lower: a for a in defaults}


AGENTS: dict[str, AgentConfig] = _load_agents()


def get_agent(name: str) -> AgentConfig | None:
    """Look up agent config by name (case-insensitive)."""
    return AGENTS.get(name.lower())


def agent_names() -> set[str]:
    """Return set of all known agent names (lowercase)."""
    return set(AGENTS.keys())


def get_url(name: str) -> str:
    """Get ACP URL for an agent, with fallback."""
    agent = AGENTS.get(name.lower())
    return agent.acp_url if agent else "http://127.0.0.1:3133"


def get_voice(name: str) -> str:
    """Get TTS voice for an agent."""
    agent = AGENTS.get(name.lower())
    return agent.voice if agent else "en-US-AriaNeural"


def get_container(name: str) -> str:
    """Get Docker container name for an agent."""
    agent = AGENTS.get(name.lower())
    return agent.container if agent else f"skynet-{name.lower()}"


def supports_streaming(name: str) -> bool:
    """Check if an agent's gateway supports SSE streaming."""
    agent = AGENTS.get(name.lower())
    return agent.streaming if agent else False
