"""Tests for shared utilities."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


class TestLoadEvalModel:
    def test_parses_provider_model(self) -> None:
        from arcagent.utils import load_eval_model

        with patch("arcagent.utils.arcllm_load_model") as mock_load:
            mock_load.return_value = MagicMock()
            result = load_eval_model("anthropic/claude-haiku")
            mock_load.assert_called_once_with("anthropic", "claude-haiku")
            assert result is not None

    def test_no_model_name(self) -> None:
        from arcagent.utils import load_eval_model

        with patch("arcagent.utils.arcllm_load_model") as mock_load:
            mock_load.return_value = MagicMock()
            load_eval_model("anthropic")
            mock_load.assert_called_once_with("anthropic", None)


class TestFormatMessages:
    def test_format_basic(self) -> None:
        from arcagent.utils.io import format_messages

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = format_messages(messages)
        assert "user: hello" in result
        assert "assistant: hi there" in result

    def test_format_with_limit(self) -> None:
        from arcagent.utils.io import format_messages

        messages = [
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "msg2"},
            {"role": "user", "content": "msg3"},
        ]
        result = format_messages(messages, limit=2)
        assert "msg1" not in result
        assert "msg2" in result
        assert "msg3" in result

    def test_format_with_type_filter(self) -> None:
        from arcagent.utils.io import format_messages

        messages = [
            {"type": "message", "role": "user", "content": "keep1"},
            {"type": "compaction_summary", "role": "system", "content": "skip"},
            {"type": "message", "role": "assistant", "content": "keep2"},
        ]
        result = format_messages(messages, type_filter="message")
        assert "keep1" in result
        assert "keep2" in result
        assert "skip" not in result


class TestSanitizeFts5Query:
    def test_sanitize_basic(self) -> None:
        from arcagent.utils.io import sanitize_fts5_query

        result = sanitize_fts5_query("hello world")
        assert result == '"hello" "world"'

    def test_sanitize_removes_special_chars(self) -> None:
        from arcagent.utils.io import sanitize_fts5_query

        result = sanitize_fts5_query("test* query{} with:")
        # Special chars removed, each term quoted
        assert '"test"' in result
        assert '"query"' in result
        assert '"with"' in result
        assert "*" not in result
        assert "{" not in result

    def test_sanitize_empty_after_cleaning(self) -> None:
        from arcagent.utils.io import sanitize_fts5_query

        # All special chars, nothing left
        result = sanitize_fts5_query("*** {}")
        assert result == ""


