"""Tests for the ACP Bus client library."""

import unittest
from acp_bus_client import AcpBusClient


class AcpBusClientTests(unittest.TestCase):

    def test_default_url(self):
        client = AcpBusClient()
        self.assertEqual(client.url, "ws://127.0.0.1:9090")

    def test_custom_url(self):
        client = AcpBusClient(url="ws://10.0.0.1:9999")
        self.assertEqual(client.url, "ws://10.0.0.1:9999")

    def test_not_connected_by_default(self):
        client = AcpBusClient()
        self.assertFalse(client.connected)

    def test_degraded_flag_initially_false(self):
        client = AcpBusClient()
        self.assertFalse(client._degraded)

    def test_publish_returns_without_connection(self):
        """publish() should not raise when disconnected."""
        import asyncio
        client = AcpBusClient()
        # Should not raise
        asyncio.get_event_loop().run_until_complete(
            client.publish("room:test", {"type": "test"})
        )

    def test_get_recent_returns_empty_without_connection(self):
        """get_recent() should return empty list when disconnected."""
        import asyncio
        client = AcpBusClient()
        result = asyncio.get_event_loop().run_until_complete(
            client.get_recent("room:test", n=5)
        )
        self.assertEqual(result, [])

    def test_subscriptions_stored(self):
        """subscribe() should store topics for reconnect."""
        import asyncio
        client = AcpBusClient()
        asyncio.get_event_loop().run_until_complete(
            client.subscribe(["room:a", "room:b"])
        )
        self.assertIn("room:a", client._subscriptions)
        self.assertIn("room:b", client._subscriptions)

    def test_subscriptions_deduplicated(self):
        """subscribe() should not add duplicate topics."""
        import asyncio
        client = AcpBusClient()
        asyncio.get_event_loop().run_until_complete(
            client.subscribe(["room:a", "room:a"])
        )
        self.assertEqual(client._subscriptions.count("room:a"), 1)

    def test_close_resets_state(self):
        import asyncio
        client = AcpBusClient()
        client._running = True
        asyncio.get_event_loop().run_until_complete(client.close())
        self.assertFalse(client._running)


if __name__ == "__main__":
    unittest.main()
