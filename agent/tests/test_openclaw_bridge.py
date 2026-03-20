import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openclaw_bridge import (  # noqa: E402
    _build_openclaw_agent_cmd,
    _evaluate_openclaw_runtime,
    _extract_openclaw_version,
)


class OpenClawBridgeTests(unittest.TestCase):
    def test_livekit_lane_isolated_by_session_id_without_agent_flag(self) -> None:
        cmd = _build_openclaw_agent_cmd(
            container="skynet-laira",
            message="ping",
            agent_id=None,
            session_id="livekit-skynet-comms",
            timeout=30,
        )

        self.assertNotIn("--agent", cmd)
        self.assertIn("--session-id", cmd)
        self.assertIn("livekit-skynet-comms", cmd)
        self.assertEqual(
            cmd,
            [
                "docker", "exec", "--user", "node", "skynet-laira",
                "openclaw", "agent",
                "--session-id", "livekit-skynet-comms",
                "--message", "ping",
                "--json",
                "--thinking", "off",
                "--timeout", "30",
            ],
        )

    def test_real_agent_override_is_explicit_when_requested(self) -> None:
        cmd = _build_openclaw_agent_cmd(
            container="skynet-loki",
            message="ping",
            agent_id="main",
            session_id="livekit-skynet-comms",
            timeout=45,
        )

        self.assertIn("--agent", cmd)
        agent_index = cmd.index("--agent")
        session_index = cmd.index("--session-id")
        message_index = cmd.index("--message")
        self.assertEqual(cmd[agent_index:agent_index + 2], ["--agent", "main"])
        self.assertEqual(cmd[session_index:session_index + 2], ["--session-id", "livekit-skynet-comms"])
        self.assertLess(agent_index, session_index)
        self.assertLess(session_index, message_index)
        self.assertEqual(cmd[-2:], ["--timeout", "45"])

    def test_extract_openclaw_version_reads_cli_output(self) -> None:
        self.assertEqual(
            _extract_openclaw_version("OpenClaw 2026.3.13 (61d171a)\n"),
            "2026.3.13",
        )
        self.assertIsNone(_extract_openclaw_version("not openclaw"))

    def test_runtime_eval_rejects_required_version_mismatch(self) -> None:
        status = _evaluate_openclaw_runtime(
            required_version="2026.3.13",
            installed_version="2026.3.8",
            config_version="2026.3.8",
        )
        self.assertFalse(status["compatible"])
        self.assertIn("required 2026.3.13, found 2026.3.8", status["reason"])

    def test_runtime_eval_rejects_config_newer_than_binary(self) -> None:
        status = _evaluate_openclaw_runtime(
            required_version=None,
            installed_version="2026.3.8",
            config_version="2026.3.13",
        )
        self.assertFalse(status["compatible"])
        self.assertIn("config 2026.3.13, installed 2026.3.8", status["reason"])

    def test_runtime_eval_accepts_pinned_matching_version(self) -> None:
        status = _evaluate_openclaw_runtime(
            required_version="2026.3.13",
            installed_version="2026.3.13",
            config_version="2026.3.13",
        )
        self.assertTrue(status["compatible"])
        self.assertEqual(status["reason"], "OpenClaw runtime is compatible")


if __name__ == "__main__":
    unittest.main()
