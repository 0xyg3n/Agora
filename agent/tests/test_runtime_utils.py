import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime_utils import (  # noqa: E402
    build_room_context,
    classify_agent_turn_trigger,
    classify_openclaw_result,
    ensure_spoken_response_text,
    is_directly_addressed,
    is_group_address,
    is_stop_command,
    is_vision_failure_text,
    mentions_name,
    normalize_context_text,
    parse_turn_count,
    should_store_context_message,
)


class RuntimeUtilsTests(unittest.TestCase):
    def test_normalize_context_text_collapses_whitespace_and_caps(self) -> None:
        self.assertEqual(
            normalize_context_text("  hello \n\n   there   friend  ", 12),
            "hello the...",
        )

    def test_should_store_context_message_filters_empty_and_low_value_lines(self) -> None:
        low_value_lines = {
            "hey, laira here!",
            "sorry, i'm having trouble right now.",
        }
        self.assertFalse(
            should_store_context_message("", low_value_lines=low_value_lines, max_entry_chars=120)
        )
        self.assertFalse(
            should_store_context_message(
                "  Sorry, I'm having trouble right now.  ",
                low_value_lines=low_value_lines,
                max_entry_chars=120,
            )
        )
        self.assertTrue(
            should_store_context_message(
                "Need to check the build logs",
                low_value_lines=low_value_lines,
                max_entry_chars=120,
            )
        )

    def test_build_room_context_keeps_recent_entries_within_budget(self) -> None:
        entries = [
            ("Laira", "first old line", 1.0),
            ("Loki", "second old line", 2.0),
            ("Laira", "third line with extra words", 3.0),
            ("Loki", "fourth line with a lot more words than fit", 4.0),
        ]

        context = build_room_context(
            entries,
            max_messages=3,
            max_chars=70,
            max_entry_chars=16,
        )

        self.assertTrue(context.startswith("[Recent room context]:\n"))
        self.assertNotIn("first old line", context)
        self.assertIn("[Loki]: fourth line w...", context)
        self.assertLessEqual(len(context), len("[Recent room context]:\n") + 70 + 2)

    def test_classify_openclaw_result_success_requires_non_empty_text(self) -> None:
        success = classify_openclaw_result(
            {"ok": True, "text": "  Working reply.  "},
            openclaw_fallback="fallback",
            timeout_fallback="timeout",
            empty_reply_fallback="empty",
        )
        empty = classify_openclaw_result(
            {"ok": True, "text": "   "},
            openclaw_fallback="fallback",
            timeout_fallback="timeout",
            empty_reply_fallback="empty",
        )

        self.assertEqual(success["spoken_text"], "Working reply.")
        self.assertTrue(success["ok"])
        self.assertEqual(empty["spoken_text"], "empty")
        self.assertFalse(empty["ok"])
        self.assertEqual(empty["status"], "OpenClaw reply was empty")

    def test_classify_openclaw_result_maps_timeout_and_bridge_failures(self) -> None:
        timeout = classify_openclaw_result(
            {"ok": False, "text": "OpenClaw timed out"},
            openclaw_fallback="fallback",
            timeout_fallback="timeout",
            empty_reply_fallback="empty",
        )
        bridge = classify_openclaw_result(
            {"ok": False, "text": "Bridge error: docker exec failed"},
            openclaw_fallback="fallback",
            timeout_fallback="timeout",
            empty_reply_fallback="empty",
        )

        self.assertEqual(timeout["spoken_text"], "timeout")
        self.assertEqual(timeout["safe_error"], "OpenClaw timed out")
        self.assertEqual(bridge["spoken_text"], "fallback")
        self.assertEqual(bridge["status"], "Bridge request failed")

    def test_ensure_spoken_response_text_guarantees_non_empty_output(self) -> None:
        self.assertEqual(
            ensure_spoken_response_text("  Ready to go. ", "fallback"),
            ("Ready to go.", False),
        )
        self.assertEqual(
            ensure_spoken_response_text("   ", "fallback"),
            ("fallback", True),
        )

    def test_is_vision_failure_text_matches_known_helper_replies(self) -> None:
        prefixes = (
            "sorry, i had trouble seeing",
            "sorry, the vision request timed out",
        )
        self.assertTrue(
            is_vision_failure_text("Sorry, I had trouble seeing that. Could you try again?", prefixes)
        )
        self.assertTrue(
            is_vision_failure_text("Sorry, the vision request timed out. Try again?", prefixes)
        )
        self.assertFalse(
            is_vision_failure_text("I don't see a screen share. Are you sharing your screen?", prefixes)
        )

    def test_mentions_name_matches_whole_word_mentions(self) -> None:
        self.assertTrue(mentions_name("Laira said Loki should jump in now.", "loki"))
        self.assertFalse(mentions_name("I like loking around", "loki"))

    def test_is_directly_addressed_accepts_start_and_sentence_end_vocatives(self) -> None:
        self.assertTrue(is_directly_addressed("Loki, tell me why seven is better.", "loki"))
        self.assertTrue(is_directly_addressed("At least 42 has personality. Loki", "loki"))
        self.assertFalse(is_directly_addressed("I think Loki is being dramatic.", "loki"))

    def test_classify_agent_turn_trigger_falls_back_to_mentions(self) -> None:
        self.assertEqual(
            classify_agent_turn_trigger("Hey Loki, say something dangerous.", "loki"),
            "direct",
        )
        self.assertEqual(
            classify_agent_turn_trigger("Laira says Loki should stop hiding.", "loki"),
            "mention",
        )
        self.assertIsNone(
            classify_agent_turn_trigger("Laira is handling this one.", "loki"),
        )


    # --- Turn-taking tests ---

    def test_parse_turn_count_explicit_turns(self) -> None:
        self.assertEqual(parse_turn_count("talk for 5 turns"), 5)
        self.assertEqual(parse_turn_count("chat for 3 rounds"), 3)
        self.assertEqual(parse_turn_count("discuss for 10 exchanges"), 10)

    def test_parse_turn_count_short_form(self) -> None:
        self.assertEqual(parse_turn_count("5 turns"), 5)
        self.assertEqual(parse_turn_count("give me 3 rounds"), 3)

    def test_parse_turn_count_each_doubles(self) -> None:
        self.assertEqual(parse_turn_count("3 turns each"), 6)
        self.assertEqual(parse_turn_count("5 rounds each"), 10)

    def test_parse_turn_count_minutes(self) -> None:
        self.assertEqual(parse_turn_count("discuss for 2 minutes"), 20)
        self.assertEqual(parse_turn_count("talk for 1 min"), 10)

    def test_parse_turn_count_caps_at_max(self) -> None:
        self.assertEqual(parse_turn_count("talk for 100 turns"), 20)
        self.assertEqual(parse_turn_count("50 turns each"), 20)

    def test_parse_turn_count_no_match(self) -> None:
        self.assertEqual(parse_turn_count("hello laira"), 0)
        self.assertEqual(parse_turn_count("what do you think?"), 0)

    def test_parse_turn_count_talk_to_each_other(self) -> None:
        self.assertEqual(parse_turn_count("talk to each other for 5 turns"), 5)

    def test_is_group_address(self) -> None:
        self.assertTrue(is_group_address("hey guys what do you think"))
        self.assertTrue(is_group_address("you two discuss this"))
        self.assertTrue(is_group_address("both of you answer"))
        self.assertTrue(is_group_address("hey everyone"))
        self.assertTrue(is_group_address("ok team"))
        self.assertFalse(is_group_address("hey laira"))
        self.assertFalse(is_group_address("loki tell me"))

    def test_is_stop_command(self) -> None:
        self.assertTrue(is_stop_command("stop"))
        self.assertTrue(is_stop_command("enough"))
        self.assertTrue(is_stop_command("ok stop talking"))
        self.assertTrue(is_stop_command("shut up"))
        self.assertTrue(is_stop_command("that's enough"))
        self.assertTrue(is_stop_command("quiet"))
        self.assertFalse(is_stop_command("don't stop believing"))
        self.assertFalse(is_stop_command("hey laira stop loki from talking"))


if __name__ == "__main__":
    unittest.main()
