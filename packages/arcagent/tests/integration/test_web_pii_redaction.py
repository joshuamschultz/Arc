"""Integration test — PII redaction for web search and extraction.

Verifies that PII in queries and extracted content is redacted before
being processed or returned, using arcllm._pii.RegexPiiDetector. Exercised
through the live web_search / web_extract capabilities + _runtime.

Scenarios:
  - Search query containing SSN is redacted before being sent to provider
  - Extracted content containing email address is redacted in returned result
  - PII in query does not appear in audit event (query_hash used instead)
  - Personal tier with pii_redaction_enabled=False bypasses redaction

These tests do NOT make real network calls — providers are injected stubs.

Spec: SPEC-018 T4.8.4
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.web import _runtime, capabilities
from arcagent.modules.web.protocols import ExtractResult, SearchHit


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _configure(
    tier: str,
    *,
    allowlist: list[str] | None = None,
    pii: bool = True,
    telemetry: Any = None,
) -> Any:
    tel = telemetry if telemetry is not None else MagicMock()
    cfg: dict[str, Any] = {"tier": tier, "pii_redaction_enabled": pii}
    if allowlist is not None:
        cfg["url_allowlist"] = allowlist
    _runtime.configure(config=cfg, telemetry=tel, agent_name="test-agent")
    return tel


def _inject_capturing_search() -> list[str]:
    captured: list[str] = []

    async def _capture(query: str, *, limit: int = 10) -> list[SearchHit]:
        captured.append(query)
        return []

    stub = MagicMock()
    stub.search = _capture
    _runtime.state().search_provider = stub
    return captured


class TestSearchQueryPiiRedaction:
    """PII in search queries must be redacted before reaching the provider."""

    async def test_ssn_in_query_redacted(self) -> None:
        _configure("federal", allowlist=["*"], pii=True)
        captured = _inject_capturing_search()

        await capabilities.web_search("Find records for SSN 123-45-6789")

        assert len(captured) == 1
        sent_query = captured[0]
        assert "123-45-6789" not in sent_query
        assert "[PII:SSN]" in sent_query

    async def test_email_in_query_redacted(self) -> None:
        _configure("enterprise", pii=True)
        captured = _inject_capturing_search()

        await capabilities.web_search("Find info about user@example.com")

        sent_query = captured[0]
        assert "user@example.com" not in sent_query
        assert "[PII:EMAIL]" in sent_query

    async def test_clean_query_passes_through_unchanged(self) -> None:
        _configure("federal", allowlist=["*"], pii=True)
        captured = _inject_capturing_search()

        clean_query = "Python async programming best practices"
        await capabilities.web_search(clean_query)

        assert captured[0] == clean_query


class TestExtractedContentPiiRedaction:
    """PII in extracted content must be redacted in the returned result."""

    async def test_email_in_content_redacted(self) -> None:
        _configure("federal", allowlist=["https://example.gov/*"], pii=True)
        raw_content = "Contact admin@example.gov for support.\n\nSee our FAQ."
        stub = MagicMock()
        stub.extract = AsyncMock(
            return_value=ExtractResult(
                url="https://example.gov/contact",
                title="Contact",
                content=raw_content,
                fetched_at=time.time(),
            )
        )
        _runtime.state().extract_provider = stub

        raw = await capabilities.web_extract("https://example.gov/contact")
        result = json.loads(raw)
        assert "admin@example.gov" not in result["content"]
        assert "[PII:EMAIL]" in result["content"]
        assert "See our FAQ" in result["content"]

    async def test_ssn_in_content_redacted(self) -> None:
        _configure(
            "enterprise",
            allowlist=["https://records.example.com/*"],
            pii=True,
        )
        raw_content = "Record found: SSN 987-65-4321 belongs to subject."
        stub = MagicMock()
        stub.extract = AsyncMock(
            return_value=ExtractResult(
                url="https://records.example.com/file",
                title="Record",
                content=raw_content,
                fetched_at=time.time(),
            )
        )
        _runtime.state().extract_provider = stub

        raw = await capabilities.web_extract("https://records.example.com/file")
        result = json.loads(raw)
        assert "987-65-4321" not in result["content"]
        assert "[PII:SSN]" in result["content"]


class TestPiiRedactionAuditPrivacy:
    """PII must not appear in audit events — only query hash."""

    async def test_audit_event_contains_hash_not_plaintext(self) -> None:
        tel = _configure("federal", allowlist=["*"], pii=True)
        stub = MagicMock()
        stub.search = AsyncMock(return_value=[])
        _runtime.state().search_provider = stub

        await capabilities.web_search("records for John SSN 123-45-6789")

        search_calls = [c for c in tel.audit_event.call_args_list if c[0][0] == "web.search"]
        assert len(search_calls) == 1
        event_data = search_calls[0][0][1]

        data_str = str(event_data)
        assert "123-45-6789" not in data_str
        assert "John" not in data_str
        assert "query_hash" in event_data
        assert len(event_data["query_hash"]) == 16


class TestPiiRedactionDisabled:
    """Personal tier with pii_redaction_enabled=False sends content as-is."""

    async def test_pii_redaction_disabled_passes_raw_query(self) -> None:
        _configure("personal", pii=False)
        captured = _inject_capturing_search()

        raw_query = "email test@example.com info"
        await capabilities.web_search(raw_query)

        assert captured[0] == raw_query
