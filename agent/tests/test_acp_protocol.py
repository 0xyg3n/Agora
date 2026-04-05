"""Tests for ACP message types and serialization."""

import time
import unittest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from acp_protocol import ACPMessage, ACPResponseChunk, ChunkType, MessageType


class ACPProtocolTests(unittest.TestCase):

    def test_message_type_values(self):
        self.assertEqual(MessageType.VOICE_INPUT.value, "voice_input")
        self.assertEqual(MessageType.TEXT_INPUT.value, "text_input")
        self.assertEqual(MessageType.AGENT_TO_AGENT.value, "agent_to_agent")

    def test_chunk_type_values(self):
        self.assertEqual(ChunkType.TEXT_CHUNK.value, "text_chunk")
        self.assertEqual(ChunkType.DONE.value, "done")
        self.assertEqual(ChunkType.ERROR.value, "error")

    def test_acp_message_to_chat_messages_without_system(self):
        msg = ACPMessage(
            type=MessageType.VOICE_INPUT,
            session_id="livekit-test",
            sender="user1",
            content="Hello world",
        )
        result = msg.to_chat_messages()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[0]["content"], "Hello world")

    def test_acp_message_to_chat_messages_with_system(self):
        msg = ACPMessage(
            type=MessageType.VOICE_INPUT,
            session_id="test",
            sender="user1",
            content="Hello",
        )
        result = msg.to_chat_messages(system_prompt="Be brief")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[0]["content"], "Be brief")
        self.assertEqual(result[1]["role"], "user")

    def test_acp_message_default_timestamp(self):
        before = time.time()
        msg = ACPMessage(
            type=MessageType.VOICE_INPUT,
            session_id="test",
            sender="u",
            content="hi",
        )
        after = time.time()
        self.assertGreaterEqual(msg.timestamp, before)
        self.assertLessEqual(msg.timestamp, after)

    def test_response_chunk_defaults(self):
        chunk = ACPResponseChunk(type=ChunkType.TEXT_CHUNK)
        self.assertEqual(chunk.content, "")
        self.assertEqual(chunk.metadata, {})

    def test_response_chunk_with_content(self):
        chunk = ACPResponseChunk(type=ChunkType.TEXT_CHUNK, content="Hello")
        self.assertEqual(chunk.content, "Hello")


if __name__ == "__main__":
    unittest.main()
