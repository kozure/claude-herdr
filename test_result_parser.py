"""Tests for result_parser.py — parses Claude interactive terminal text.

Extracts the assistant's response from terminal output containing UI markers:
  ⏺ = assistant message prefix
  ❯ = user prompt prefix  
  ✻ = status line prefix
"""
import pytest
from result_parser import parse_result


class TestResultParser:
    """Test extraction of assistant responses from terminal text."""

    def test_simple_response(self):
        """Extract a single ⏺ block after the last ❯ prompt."""
        text = "❯ Reply with exactly: HERDR_OK\n⏺ HERDR_OK\n"
        result = parse_result(text)
        assert "HERDR_OK" in result["text"]
        assert result.get("parse_warning") is None

    def test_multiline_response(self):
        """Extract a multi-line ⏺ block."""
        text = "❯ do something\n⏺ Line 1 of response\nLine 2 of response\nLine 3 of response\n"
        result = parse_result(text)
        assert "Line 1" in result["text"]
        assert "Line 2" in result["text"]
        assert "Line 3" in result["text"]

    def test_multiple_turns(self):
        """Should extract only the LAST ⏺ block (after the last ❯)."""
        text = (
            "❯ first prompt\n"
            "⏺ first response\n"
            "❯ second prompt\n"
            "⏺ second response\n"
        )
        result = parse_result(text)
        assert "second response" in result["text"]
        assert "first response" not in result["text"]

    def test_ansi_strip(self):
        """Should strip ANSI escape codes from the text."""
        # ANSI color code: \x1b[32m green \x1b[0m
        text = "❯ prompt\n⏺ \x1b[32mgreen text\x1b[0m more text\n"
        result = parse_result(text)
        assert "\x1b" not in result["text"]
        assert "green text" in result["text"]
        assert "more text" in result["text"]

    def test_status_lines_ignored(self):
        """✻ status lines should not appear in the result."""
        text = "❯ prompt\n⏺ response here\n✻ Status: doing something\n"
        result = parse_result(text)
        # The ⏺ block is just "response here" — the ✻ line is a status line
        assert "response here" in result["text"]
        assert "Status: doing something" not in result["text"]

    def test_empty_transcript(self):
        """Empty text → empty result with parse_warning."""
        result = parse_result("")
        assert result["text"] == ""
        assert result["parse_warning"] == "markers_not_found"

    def test_no_markers(self):
        """Text without ⏺/❯ markers → fallback with raw text + warning."""
        text = "Some random terminal output without markers\n"
        result = parse_result(text)
        assert result["text"] == "Some random terminal output without markers"
        assert result["parse_warning"] == "markers_not_found"

    def test_prompt_only_no_response(self):
        """❯ prompt but no ⏺ response → fallback with raw text + warning."""
        text = "❯ a prompt with no response yet\n"
        result = parse_result(text)
        assert result["parse_warning"] == "markers_not_found"
        # Fallback returns the raw text (stripped) since no ⏺ block found
        assert "a prompt" in result["text"]

    def test_raw_terminal_preserved(self):
        """raw_terminal should always contain the original input."""
        text = "❯ prompt\n⏺ response\n"
        result = parse_result(text)
        assert result["raw_terminal"] == text

    def test_model_info_extracted(self):
        """If model info is present in the text, extract it."""
        # Claude sometimes shows model info in status lines
        text = "❯ prompt\n⏺ response\n"
        result = parse_result(text)
        # model_info may be None if not found
        assert "model_info" in result

    def test_multiple_ansi_codes(self):
        """Multiple ANSI codes in one line."""
        text = "❯ prompt\n⏺ \x1b[1;32mbold green\x1b[0m and \x1b[31mred\x1b[0m\n"
        result = parse_result(text)
        assert "\x1b" not in result["text"]
        assert "bold green" in result["text"]
        assert "red" in result["text"]

    def test_emoji_and_unicode(self):
        """Should handle emoji and unicode in the response."""
        text = "❯ prompt\n⏺ Here's a response with emoji 🎉 and unicode — café\n"
        result = parse_result(text)
        assert "🎉" in result["text"]
        assert "café" in result["text"]

    def test_strips_leading_marker(self):
        """The ⏺ marker itself should not be in the extracted text."""
        text = "❯ prompt\n⏺ the response\n"
        result = parse_result(text)
        assert "⏺" not in result["text"]
        assert result["text"].strip() == "the response"

    def test_strips_prompt_marker(self):
        """The ❯ marker should not be in the extracted text."""
        text = "❯ prompt text\n⏺ response\n"
        result = parse_result(text)
        # The prompt is not part of the response
        assert "❯" not in result["text"]

    def test_whitespace_handling(self):
        """Should strip excess whitespace from the result."""
        text = "❯ prompt\n⏺   response with leading/trailing spaces   \n\n\n"
        result = parse_result(text)
        assert result["text"].strip() == "response with leading/trailing spaces"

    def test_trailing_idle_prompt(self):
        """After completion, Claude shows a new ❯ idle prompt — should still
        extract the last ⏺ response, not the empty idle prompt."""
        text = (
            "❯ Reply with exactly: TEST\n"
            "⏺ TEST\n"
            "✻ Brewed for 1s · done 2:45 PM\n"
            "\n"
            "❯ \n"  # trailing idle prompt with no response
        )
        result = parse_result(text)
        assert result["text"] == "TEST"
        assert result["parse_warning"] is None

    def test_trailing_idle_prompt_multiline_response(self):
        """Multi-line response with trailing idle prompt."""
        text = (
            "❯ do something\n"
            "⏺ Line 1\n"
            "Line 2\n"
            "✻ Done\n"
            "❯ \n"  # trailing idle prompt
        )
        result = parse_result(text)
        assert "Line 1" in result["text"]
        assert "Line 2" in result["text"]
        assert result["parse_warning"] is None