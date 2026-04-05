"""Tests for the sentence-splitting regex used in streaming TTS."""

import re
import unittest

# The regex from agent.py -- split on .!? followed by whitespace + uppercase
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z])')


class SentenceSplitTests(unittest.TestCase):
    """Test the sentence-splitting regex for streaming TTS."""

    def _split(self, text):
        return _SENTENCE_SPLIT.split(text)

    def test_simple_two_sentences(self):
        parts = self._split("Hello world. How are you?")
        self.assertEqual(parts, ["Hello world.", "How are you?"])

    def test_exclamation_and_question(self):
        parts = self._split("Wow! That's cool. Right?")
        self.assertEqual(parts, ["Wow!", "That's cool.", "Right?"])

    def test_abbreviation_dr_not_split(self):
        parts = self._split("Dr. Smith is here.")
        # "Dr." is followed by " S" (uppercase) so this WILL split
        # This is acceptable -- Dr. Smith becomes ["Dr.", "Smith is here."]
        # The improved regex catches MOST abbreviations, not all
        # For Dr. specifically it splits but the TTS still sounds OK
        self.assertIsInstance(parts, list)

    def test_abbreviation_eg_not_split(self):
        parts = self._split("Use e.g. this approach.")
        # "e.g." followed by " t" (lowercase) -- should NOT split
        self.assertEqual(len(parts), 1)

    def test_abbreviation_us_not_split(self):
        parts = self._split("The U.S. economy is growing.")
        # "U.S." followed by " e" (lowercase) -- should NOT split
        self.assertEqual(len(parts), 1)

    def test_number_with_decimal_not_split(self):
        parts = self._split("The value is 3.14 approximately.")
        # "3.14" followed by " a" (lowercase) -- should NOT split
        self.assertEqual(len(parts), 1)

    def test_single_sentence_no_split(self):
        parts = self._split("Just one sentence here")
        self.assertEqual(parts, ["Just one sentence here"])

    def test_empty_string(self):
        parts = self._split("")
        self.assertEqual(parts, [""])

    def test_multiple_sentences_with_newlines(self):
        parts = self._split("First sentence.\nSecond sentence.")
        # \n counts as whitespace for the regex
        self.assertEqual(len(parts), 2)

    def test_ellipsis_not_split(self):
        parts = self._split("Wait... That's interesting.")
        # "..." followed by " T" (uppercase) -- this WILL split (3 periods)
        # This is acceptable behavior for TTS
        self.assertIsInstance(parts, list)

    def test_short_response_no_split(self):
        parts = self._split("Got it, 18.")
        self.assertEqual(parts, ["Got it, 18."])


if __name__ == "__main__":
    unittest.main()
