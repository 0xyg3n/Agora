"""Tests for cross-session context sync."""

import json
import os
import tempfile
import unittest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import acp_context_sync


class ACPContextSyncTests(unittest.TestCase):

    def setUp(self):
        self._tmpfile = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        )
        self._tmpfile.close()
        self._orig_path = acp_context_sync.CONTEXT_SYNC_PATH
        acp_context_sync.CONTEXT_SYNC_PATH = type(self._orig_path)(self._tmpfile.name)
        # Reset in-memory state
        acp_context_sync._line_count = 0
        acp_context_sync._line_count_loaded = False

    def tearDown(self):
        acp_context_sync.CONTEXT_SYNC_PATH = self._orig_path
        try:
            os.unlink(self._tmpfile.name)
        except FileNotFoundError:
            pass

    def _read_lines(self):
        import time
        time.sleep(0.1)  # let daemon thread finish
        with open(self._tmpfile.name, "r") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_publish_event_writes_jsonl(self):
        acp_context_sync.publish_event(
            room="test-room",
            speaker="Alice",
            agent="laira",
            event_type="voice_input",
            content="Hello world",
        )
        lines = self._read_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["room"], "test-room")
        self.assertEqual(lines[0]["speaker"], "Alice")
        self.assertEqual(lines[0]["agent"], "laira")
        self.assertEqual(lines[0]["content"], "Hello world")

    def test_publish_event_skips_empty_content(self):
        acp_context_sync.publish_event(
            room="r", speaker="s", agent="a", event_type="t", content=""
        )
        acp_context_sync.publish_event(
            room="r", speaker="s", agent="a", event_type="t", content="   "
        )
        import time; time.sleep(0.1)
        with open(self._tmpfile.name, "r") as f:
            self.assertEqual(f.read().strip(), "")

    def test_publish_event_truncates_long_content(self):
        acp_context_sync.publish_event(
            room="r", speaker="s", agent="a", event_type="t",
            content="x" * 1000,
        )
        lines = self._read_lines()
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]["content"]), 500)

    def test_read_recent_returns_last_n(self):
        for i in range(5):
            acp_context_sync.publish_event(
                room="r", speaker="s", agent="a",
                event_type="t", content=f"msg-{i}",
            )
        import time; time.sleep(0.2)
        events = acp_context_sync.read_recent(n=3)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[-1]["content"], "msg-4")

    def test_read_recent_with_room_filter(self):
        acp_context_sync.publish_event(
            room="room-a", speaker="s", agent="a", event_type="t", content="a1",
        )
        acp_context_sync.publish_event(
            room="room-b", speaker="s", agent="a", event_type="t", content="b1",
        )
        import time; time.sleep(0.2)
        events = acp_context_sync.read_recent(n=10, room="room-a")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["content"], "a1")

    def test_read_recent_returns_empty_for_missing_file(self):
        acp_context_sync.CONTEXT_SYNC_PATH = type(self._orig_path)("/tmp/nonexistent_test_file.jsonl")
        self.assertEqual(acp_context_sync.read_recent(), [])


if __name__ == "__main__":
    unittest.main()
