"""Unit tests for TavilyProvider — mocked httpx.

Verifies:
- search() parses Tavily response format with score
- extract() parses Tavily extract response
- API key is sent in request body (not Authorization header)
- HTTP 4xx/5xx raises appropriate errors
- Network errors are wrapped correctly
- Empty API key raises ProviderConfigMissing
- Client reuse across calls (SPEC-018 Wave B1)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from arcagent.modules.web.errors import ExtractFailed, ProviderConfigMissing, SearchFailed
from arcagent.modules.web.protocols import ExtractResult, SearchHit
from arcagent.modules.web.providers.tavily import TavilyProvider


def _make_mock_response(status: int, body: dict) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.text = json.dumps(body)
    resp.json.return_value = body
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def _make_client_mock(response: MagicMock) -> AsyncMock:
    """Build a mock httpx.AsyncClient whose post() returns response."""
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.aclose = AsyncMock()
    return client


class TestTavilyProviderConstruction:
    def test_empty_api_key_raises(self) -> None:
        with pytest.raises(ProviderConfigMissing):
            TavilyProvider(api_key="")

    def test_create_factory(self) -> None:
        provider = TavilyProvider.create(api_key="tvly-key")
        assert provider._api_key == "tvly-key"

    def test_auth_payload_contains_api_key(self) -> None:
        provider = TavilyProvider.create(api_key="tvly-key")
        payload = provider._auth_payload()
        assert payload["api_key"] == "tvly-key"


class TestTavilySearch:
    async def test_search_parses_results_with_score(self) -> None:
        response_body = {
            "results": [
                {"url": "https://a.com", "title": "A", "content": "Content A", "score": 0.85},
                {"url": "https://b.com", "title": "B", "content": "Content B"},
            ]
        }
        provider = TavilyProvider.create(api_key="key")
        mock_client = _make_client_mock(_make_mock_response(200, response_body))

        with patch.object(provider, "_get_client", return_value=mock_client):
            results = await provider.search("test query", limit=5)

        assert len(results) == 2
        assert isinstance(results[0], SearchHit)
        assert results[0].url == "https://a.com"
        assert results[0].snippet == "Content A"
        assert results[0].score == pytest.approx(0.85)
        assert results[1].score is None

    async def test_search_sends_api_key_in_body(self) -> None:
        """Tavily uses api_key in POST body, not Authorization header."""
        provider = TavilyProvider.create(api_key="tvly-secret")
        mock_client = _make_client_mock(_make_mock_response(200, {"results": []}))

        with patch.object(provider, "_get_client", return_value=mock_client):
            await provider.search("query")

        call_kwargs = mock_client.post.call_args
        payload = call_kwargs[1].get("json", {})
        assert payload.get("api_key") == "tvly-secret"

    async def test_search_sends_correct_params(self) -> None:
        provider = TavilyProvider.create(api_key="key")
        mock_client = _make_client_mock(_make_mock_response(200, {"results": []}))

        with patch.object(provider, "_get_client", return_value=mock_client):
            await provider.search("my query", limit=7)

        call_kwargs = mock_client.post.call_args
        payload = call_kwargs[1].get("json", {})
        assert payload["query"] == "my query"
        assert payload["max_results"] == 7

    async def test_search_http_error_raises(self) -> None:
        provider = TavilyProvider.create(api_key="key")
        mock_client = _make_client_mock(_make_mock_response(401, {"detail": "Unauthorized"}))

        with patch.object(provider, "_get_client", return_value=mock_client):
            with pytest.raises(SearchFailed):
                await provider.search("query")

    async def test_search_network_error_raises(self) -> None:
        provider = TavilyProvider.create(api_key="key")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused", request=MagicMock())
        )

        with patch.object(provider, "_get_client", return_value=mock_client):
            with pytest.raises(SearchFailed):
                await provider.search("query")


class TestTavilyExtract:
    async def test_extract_parses_raw_content(self) -> None:
        response_body = {
            "results": [
                {
                    "url": "https://example.com",
                    "title": "Example Title",
                    "raw_content": "# Markdown content\n\nBody text",
                }
            ]
        }
        provider = TavilyProvider.create(api_key="key")
        mock_client = _make_client_mock(_make_mock_response(200, response_body))

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.extract("https://example.com")

        assert isinstance(result, ExtractResult)
        assert result.title == "Example Title"
        assert "Markdown content" in result.content
        # Tavily does not return links in extract
        assert result.links == []

    async def test_extract_sends_api_key_in_body(self) -> None:
        provider = TavilyProvider.create(api_key="tvly-secret")
        response_body = {"results": [{"url": "https://x.com", "raw_content": ""}]}
        mock_client = _make_client_mock(_make_mock_response(200, response_body))

        with patch.object(provider, "_get_client", return_value=mock_client):
            await provider.extract("https://x.com")

        call_kwargs = mock_client.post.call_args
        payload = call_kwargs[1].get("json", {})
        assert payload.get("api_key") == "tvly-secret"
        assert "https://x.com" in payload.get("urls", [])

    async def test_extract_matches_url_from_results(self) -> None:
        response_body = {
            "results": [
                {"url": "https://other.com", "raw_content": "other content"},
                {"url": "https://target.com", "title": "Target", "raw_content": "target content"},
            ]
        }
        provider = TavilyProvider.create(api_key="key")
        mock_client = _make_client_mock(_make_mock_response(200, response_body))

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.extract("https://target.com")

        assert result.content == "target content"

    async def test_extract_http_error_raises(self) -> None:
        provider = TavilyProvider.create(api_key="key")
        mock_client = _make_client_mock(_make_mock_response(500, {"error": "Internal error"}))

        with patch.object(provider, "_get_client", return_value=mock_client):
            with pytest.raises(ExtractFailed):
                await provider.extract("https://example.com")

    async def test_extract_network_error_raises(self) -> None:
        provider = TavilyProvider.create(api_key="key")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.TimeoutException("timeout", request=MagicMock())
        )

        with patch.object(provider, "_get_client", return_value=mock_client):
            with pytest.raises(ExtractFailed):
                await provider.extract("https://example.com")


class TestTavilyClientReuse:
    async def test_client_reuse_across_calls(self) -> None:
        """httpx.AsyncClient constructor must be called at most once (SPEC-018 Wave B1).

        Two search() calls on the same provider instance must share a single
        client — no per-request TCP/TLS handshake overhead.
        """
        provider = TavilyProvider.create(api_key="tvly-key")
        mock_response = _make_mock_response(200, {"results": []})

        constructor_call_count = 0
        shared_client = AsyncMock()
        shared_client.post = AsyncMock(return_value=mock_response)
        shared_client.aclose = AsyncMock()

        original_cls = httpx.AsyncClient

        def counting_constructor(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal constructor_call_count
            constructor_call_count += 1
            return shared_client

        with patch("arcagent.modules.web.providers.tavily.httpx.AsyncClient", counting_constructor):
            await provider.search("first query")
            await provider.search("second query")

        assert constructor_call_count == 1, (
            f"httpx.AsyncClient was constructed {constructor_call_count} times; "
            "expected 1 (client should be reused across calls)"
        )

    async def test_close_releases_client(self) -> None:
        """close() must aclose the client and set _client to None."""
        provider = TavilyProvider.create(api_key="tvly-key")
        mock_response = _make_mock_response(200, {"results": []})
        shared_client = AsyncMock()
        shared_client.post = AsyncMock(return_value=mock_response)
        shared_client.aclose = AsyncMock()

        with patch("arcagent.modules.web.providers.tavily.httpx.AsyncClient", return_value=shared_client):
            await provider.search("query")
            assert provider._client is not None
            await provider.close()
            assert provider._client is None
            shared_client.aclose.assert_called_once()
