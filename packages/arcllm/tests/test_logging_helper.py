"""Tests for shared structured logging helper."""

import logging

import pytest

from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules._logging import _sanitize, log_structured, validate_log_level


class TestSanitize:
    def test_sanitizes_newlines(self):
        assert _sanitize("hello\nworld") == "hello\\nworld"

    def test_sanitizes_carriage_return(self):
        assert _sanitize("hello\rworld") == "hello\\rworld"

    def test_sanitizes_tabs(self):
        assert _sanitize("hello\tworld") == "hello\\tworld"

    def test_sanitizes_combined(self):
        assert _sanitize("a\nb\rc\td") == "a\\nb\\rc\\td"

    def test_passthrough_clean_string(self):
        assert _sanitize("clean-model-name") == "clean-model-name"

    def test_coerces_non_string(self):
        assert _sanitize(42) == "42"


class TestLogStructured:
    def test_basic_log_line(self, caplog):
        test_logger = logging.getLogger("test.structured")
        with caplog.at_level(logging.INFO, logger="test.structured"):
            log_structured(test_logger, logging.INFO, "Test", key1="val1", key2=123)
        assert "Test | key1=val1 key2=123" in caplog.text

    def test_none_values_omitted(self, caplog):
        test_logger = logging.getLogger("test.structured")
        with caplog.at_level(logging.INFO, logger="test.structured"):
            log_structured(
                test_logger, logging.INFO, "Test",
                present="yes", missing=None, also_here=1,
            )
        assert "present=yes" in caplog.text
        assert "also_here=1" in caplog.text
        assert "missing" not in caplog.text

    def test_float_formatted_6_decimals(self, caplog):
        test_logger = logging.getLogger("test.structured")
        with caplog.at_level(logging.INFO, logger="test.structured"):
            log_structured(test_logger, logging.INFO, "Test", cost_usd=0.00105)
        assert "cost_usd=0.001050" in caplog.text

    def test_string_values_sanitized(self, caplog):
        test_logger = logging.getLogger("test.structured")
        with caplog.at_level(logging.INFO, logger="test.structured"):
            log_structured(
                test_logger, logging.INFO, "Test",
                model="evil\nINJECTED",
            )
        assert "model=evil\\nINJECTED" in caplog.text
        assert "evil\nINJECTED" not in caplog.text

    def test_respects_log_level(self, caplog):
        test_logger = logging.getLogger("test.structured")
        with caplog.at_level(logging.WARNING, logger="test.structured"):
            log_structured(test_logger, logging.DEBUG, "Test", key="val")
        assert caplog.text == ""


class TestValidateLogLevel:
    def test_default_is_info(self):
        level = validate_log_level({})
        assert level == logging.INFO

    def test_explicit_debug(self):
        level = validate_log_level({"log_level": "DEBUG"})
        assert level == logging.DEBUG

    def test_all_valid_levels(self):
        for name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            level = validate_log_level({"log_level": name})
            assert level == getattr(logging, name)

    def test_invalid_level_rejected(self):
        with pytest.raises(ArcLLMConfigError, match="Invalid log_level"):
            validate_log_level({"log_level": "NOPE"})

    def test_custom_default(self):
        level = validate_log_level({}, default="WARNING")
        assert level == logging.WARNING
