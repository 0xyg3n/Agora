"""Tests for the ACP bridge module."""

import os
import unittest

# Set env vars before import to avoid side effects
os.environ.setdefault("ACP_LAIRA_URL", "http://127.0.0.1:3133")
os.environ.setdefault("ACP_LOKI_URL", "http://127.0.0.1:8642")

from acp_bridge import (
    _build_headers,
    _get_gateway_url,
    _room_from_session_id,
    _trim,
)


class ACPBridgeTests(unittest.TestCase):

    def test_build_headers_contains_required_fields(self):
        headers = _build_headers("test-session-123")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["X-Hermes-Session-Id"], "test-session-123")
        self.assertEqual(headers["X-Hermes-Platform"], "agora")

    def test_build_headers_sanitizes_session_id(self):
        headers = _build_headers("test\r\ninjection: evil")
        safe = headers["X-Hermes-Session-Id"]
        self.assertNotIn("\r", safe)
        self.assertNotIn("\n", safe)
        self.assertNotIn(":", safe)

    def test_build_headers_truncates_long_session_id(self):
        long_id = "a" * 200
        headers = _build_headers(long_id)
        self.assertLessEqual(len(headers["X-Hermes-Session-Id"]), 128)

    def test_get_gateway_url_returns_string(self):
        url = _get_gateway_url("laira")
        self.assertTrue(url.startswith("http"))

    def test_get_gateway_url_unknown_agent_returns_fallback(self):
        url = _get_gateway_url("nonexistent-agent")
        self.assertTrue(url.startswith("http"))

    def test_room_from_session_id_extracts_room(self):
        room = _room_from_session_id("livekit-my-room")
        self.assertEqual(room, "my-room")

    def test_room_from_session_id_returns_none_for_invalid(self):
        self.assertIsNone(_room_from_session_id("something-else"))
        self.assertIsNone(_room_from_session_id(None))

    def test_trim_collapses_whitespace(self):
        result = _trim("  hello   world  \n  test  ")
        self.assertEqual(result, "hello world test")

    def test_trim_respects_limit(self):
        result = _trim("a" * 500, limit=100)
        self.assertEqual(len(result), 100)


if __name__ == "__main__":
    unittest.main()
