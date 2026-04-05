"""Centralized agent configuration registry.

All agent-specific config (URL, voice, container, streaming, greeting, delay)
is defined here.  Adding a new agent only requires adding an entry — no code
changes in agent.py, acp_bridge.py, or openclaw_bridge.py.

Config is loaded from environment variables at import time:
  AGENT_<NAME>_URL, AGENT_<NAME>_VOICE, AGENT_<NAME>_CONTAINER,
  AGENT_<NAME>_STREAMING, AGENT_<NAME>_GREETING, AGENT_<NAME>_DELAY

Or use the legacy per-agent env vars (ACP_LAIRA_URL, EDGE_TTS_VOICE_LAIRA, etc.)
for backward compatibility.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentConfig:
    """Configuration for a single agent."""
    name: str
    container: str
    acp_url: str
    voice: str
    streaming: bool = True
    greeting: str = ""
    delay: float = 1.0          # Turn-taking stagger delay (seconds)
    primary: bool = False       # Primary agent echoes transcriptions, wins tiebreaks

    @property
    def name_lower(self) -> str:
        return self.name.lower()


def _env(name: str, key: str, default: str = "") -> str:
    """Read AGENT_<NAME>_<KEY> or legacy env var."""
    upper = name.upper()
    return os.getenv(f"AGENT_{upper}_{key}", "").strip() or default


def _load_agents() -> dict[str, AgentConfig]:
    """Load agent configs from defaults + env var overrides."""
    # Default agents (backward compatible with existing .env vars)
    defaults = [
        AgentConfig(
            name="Laira",
            container=_env("Laira", "CONTAINER", "skynet-laira"),
            acp_url=os.getenv("ACP_LAIRA_URL", _env("Laira", "URL", "http://127.0.0.1:3133")),
            voice=os.getenv("EDGE_TTS_VOICE_LAIRA", _env("Laira", "VOICE", "de-DE-SeraphinaMultilingualNeural")),
            streaming=True,
            greeting=_env("Laira", "GREETING", "Hey, Laira here!"),
            delay=float(_env("Laira", "DELAY", "0.5")),
            primary=True,
        ),
        AgentConfig(
            name="Loki",
            container=_env("Loki", "CONTAINER", "skynet-loki"),
            acp_url=os.getenv("ACP_LOKI_URL", _env("Loki", "URL", "http://172.20.0.3:8642")),
            voice=os.getenv("EDGE_TTS_VOICE_LOKI", _env("Loki", "VOICE", "en-US-GuyNeural")),
            streaming=True,
            greeting=_env("Loki", "GREETING", "Yo, Loki in the house."),
            delay=float(_env("Loki", "DELAY", "3.5")),
            primary=False,
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


def get_greeting(name: str) -> str:
    """Get the static greeting for an agent."""
    agent = AGENTS.get(name.lower())
    return agent.greeting if agent and agent.greeting else f"Hey, {name} here!"


def get_delay(name: str) -> float:
    """Get turn-taking stagger delay for an agent."""
    agent = AGENTS.get(name.lower())
    return agent.delay if agent else 1.0


def is_primary(name: str) -> bool:
    """Check if this is the primary agent (wins tiebreaks, echoes transcriptions)."""
    agent = AGENTS.get(name.lower())
    return agent.primary if agent else False


def get_gateway_urls() -> dict[str, str]:
    """Return agent_name -> URL map for the ACP bridge."""
    return {name: a.acp_url.rstrip("/") for name, a in AGENTS.items()}


def get_container_map() -> dict[str, str]:
    """Return agent_name -> container name map for the OpenClaw bridge."""
    return {name: a.container for name, a in AGENTS.items()}
