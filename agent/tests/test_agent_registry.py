"""Tests for centralized agent registry."""

import os
import unittest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_registry import (
    AGENTS, get_agent, agent_names, get_url, get_voice,
    get_container, supports_streaming, AgentConfig,
)


class AgentRegistryTests(unittest.TestCase):

    def test_default_agents_exist(self):
        self.assertIn("laira", AGENTS)
        self.assertIn("loki", AGENTS)

    def test_get_agent_case_insensitive(self):
        self.assertIsNotNone(get_agent("Laira"))
        self.assertIsNotNone(get_agent("LAIRA"))
        self.assertIsNotNone(get_agent("laira"))

    def test_get_agent_unknown_returns_none(self):
        self.assertIsNone(get_agent("unknown_agent"))

    def test_agent_names_returns_set(self):
        names = agent_names()
        self.assertIsInstance(names, set)
        self.assertIn("laira", names)
        self.assertIn("loki", names)

    def test_get_url_returns_string(self):
        url = get_url("laira")
        self.assertTrue(url.startswith("http"))

    def test_get_url_fallback_for_unknown(self):
        url = get_url("nobody")
        self.assertTrue(url.startswith("http"))

    def test_get_voice_laira(self):
        voice = get_voice("laira")
        self.assertIn("Multilingual", voice)

    def test_get_voice_loki(self):
        voice = get_voice("loki")
        self.assertIn("Guy", voice)

    def test_get_container(self):
        self.assertEqual(get_container("laira"), "skynet-laira")
        self.assertEqual(get_container("loki"), "skynet-loki")
        self.assertEqual(get_container("unknown"), "skynet-unknown")

    def test_streaming_support(self):
        self.assertTrue(supports_streaming("laira"))
        self.assertTrue(supports_streaming("loki"))

    def test_agent_config_frozen(self):
        agent = get_agent("laira")
        with self.assertRaises(AttributeError):
            agent.name = "something_else"


if __name__ == "__main__":
    unittest.main()
