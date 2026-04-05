"""Tests for the TTS sanitizer function in agent.py."""

import re
import unittest

# Replicate the sanitizer regexes and function from agent.py
_CODE_BLOCK_RE = re.compile(r'```[\s\S]*?```', re.MULTILINE)
_INLINE_CODE_RE = re.compile(r'`[^`]+`')
_URL_RE = re.compile(r'https?://\S+')
_TERMINAL_LINE_RE = re.compile(r'^[\s]*[$#>].*$', re.MULTILINE)
_CURL_CMD_RE = re.compile(r'curl\s+-?\S.*', re.IGNORECASE)
_JSON_BLOCK_RE = re.compile(r'\{[^}]*"[^"]*"[^}]*\}')
_MARKDOWN_HEADER_RE = re.compile(r'^#{1,6}\s+', re.MULTILINE)
_MARKDOWN_BOLD_RE = re.compile(r'\*\*([^*]+)\*\*')
_MARKDOWN_LIST_RE = re.compile(r'^\s*[-*]\s+', re.MULTILINE)


def _sanitize_for_tts(text: str) -> str:
    if not text:
        return text
    t = _CODE_BLOCK_RE.sub('', text)
    t = _INLINE_CODE_RE.sub('', t)
    t = _URL_RE.sub('', t)
    t = _TERMINAL_LINE_RE.sub('', t)
    t = _CURL_CMD_RE.sub('', t)
    t = _JSON_BLOCK_RE.sub('', t)
    t = _MARKDOWN_HEADER_RE.sub('', t)
    t = _MARKDOWN_BOLD_RE.sub(r'\1', t)
    t = _MARKDOWN_LIST_RE.sub('', t)
    t = re.sub(r'\n{2,}', '. ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


class TTSSanitizerTests(unittest.TestCase):

    def test_plain_text_unchanged(self):
        self.assertEqual(_sanitize_for_tts("Hello, how are you?"), "Hello, how are you?")

    def test_strips_code_blocks(self):
        text = "Here is some code:\n```python\nprint('hello')\n```\nThat was it."
        result = _sanitize_for_tts(text)
        self.assertNotIn("```", result)
        self.assertNotIn("print", result)
        self.assertIn("That was it", result)

    def test_strips_inline_code(self):
        text = "Run `pip install agora` to install."
        result = _sanitize_for_tts(text)
        self.assertNotIn("`", result)
        self.assertNotIn("pip install", result)
        self.assertIn("Run", result)

    def test_strips_urls(self):
        text = "Check out https://github.com/example for more info."
        result = _sanitize_for_tts(text)
        self.assertNotIn("https://", result)
        self.assertIn("Check out", result)

    def test_strips_curl_commands(self):
        text = "I sent it with curl -s -X POST https://api.telegram.org/bot123/sendMessage"
        result = _sanitize_for_tts(text)
        self.assertNotIn("curl", result)
        self.assertNotIn("telegram.org", result)

    def test_strips_terminal_lines(self):
        text = "Output:\n$ ls -la\n> /home/user\nDone."
        result = _sanitize_for_tts(text)
        self.assertNotIn("ls -la", result)
        self.assertIn("Done", result)

    def test_strips_json(self):
        text = 'Response: {"status": "ok", "count": 5} received.'
        result = _sanitize_for_tts(text)
        self.assertNotIn('"status"', result)
        self.assertIn("received", result)

    def test_strips_markdown_headers(self):
        text = "## Section Title\nSome content here."
        result = _sanitize_for_tts(text)
        self.assertNotIn("##", result)
        self.assertIn("content here", result)

    def test_strips_markdown_bold(self):
        text = "This is **important** text."
        result = _sanitize_for_tts(text)
        self.assertNotIn("**", result)
        self.assertIn("important", result)

    def test_strips_markdown_lists(self):
        text = "Items:\n- First item\n- Second item\nEnd."
        result = _sanitize_for_tts(text)
        self.assertNotIn("- ", result)
        self.assertIn("First item", result)

    def test_empty_string(self):
        self.assertEqual(_sanitize_for_tts(""), "")

    def test_none_returns_none(self):
        self.assertIsNone(_sanitize_for_tts(None))

    def test_collapses_whitespace(self):
        text = "Hello    world\n\n\ntest"
        result = _sanitize_for_tts(text)
        self.assertNotIn("  ", result)

    def test_mixed_code_and_text(self):
        text = "Done! I sent it.\n```\ncurl -X POST ...\n```\nMessage delivered."
        result = _sanitize_for_tts(text)
        self.assertIn("Done", result)
        self.assertIn("Message delivered", result)
        self.assertNotIn("curl", result)


if __name__ == "__main__":
    unittest.main()
