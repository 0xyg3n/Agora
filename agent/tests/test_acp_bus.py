"""Tests for ACP Event Bus server and client."""

import asyncio
import unittest

from acp_bus import _buffers, _subscribers, _get_buffer, _stamp_event, MAX_EVENTS


class AcpBusUnitTests(unittest.TestCase):
    """Unit tests for bus internals (no WebSocket needed)."""

    def setUp(self):
        _buffers.clear()
        _subscribers.clear()

    def test_get_buffer_creates_deque(self):
        buf = _get_buffer("room:test")
        self.assertIsNotNone(buf)
        self.assertEqual(len(buf), 0)

    def test_get_buffer_returns_same_deque(self):
        buf1 = _get_buffer("room:test")
        buf2 = _get_buffer("room:test")
        self.assertIs(buf1, buf2)

    def test_buffer_respects_max_events(self):
        buf = _get_buffer("room:test")
        for i in range(MAX_EVENTS + 50):
            buf.append({"i": i})
        self.assertEqual(len(buf), MAX_EVENTS)
        # Oldest events should be gone
        self.assertEqual(buf[0]["i"], 50)

    def test_stamp_event_adds_timestamp(self):
        event = {"type": "test"}
        stamped = _stamp_event(event)
        self.assertIn("ts", stamped)
        self.assertIsInstance(stamped["ts"], float)

    def test_stamp_event_preserves_existing_ts(self):
        event = {"type": "test", "ts": 12345.0}
        stamped = _stamp_event(event)
        self.assertEqual(stamped["ts"], 12345.0)

    def test_different_topics_have_separate_buffers(self):
        buf1 = _get_buffer("room:a")
        buf2 = _get_buffer("room:b")
        buf1.append({"x": 1})
        self.assertEqual(len(buf1), 1)
        self.assertEqual(len(buf2), 0)


if __name__ == "__main__":
    unittest.main()
