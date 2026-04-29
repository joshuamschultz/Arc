"""Unit tests for FirecrawlProvider — mocked httpx.

Verifies:
- search() parses Firecrawl v1 response format
- extract() uses scrape endpoint with markdown format
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
from arcagent.modules.web.providers.firecrawl import FirecrawlProvider


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
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.aclose = AsyncMock()
    return client


class TestFirecrawlProviderConstruction:
    def test_empty_api_key_raises(self) -> None:
        with pytest.raises(ProviderConfigMissing):
            FirecrawlProvider(api_key="")

    def test_create_factory(self) -> None:
        provider = FirecrawlProvider.create(api_key="fc-key")
        assert provider._api_key == "fc-key"

    def test_headers_contain_bearer(self) -> None:
        provider = FirecrawlProvider.create(api_key="fc-key")
        headers = provider._headers()
        assert "Bearer fc-key" == headers["Authorization"]


class TestFirecrawlSearch:
    async def test_search_parses_v1_format(self) -> None:
        response_body = {
            "data": [
                {"url": "https://a.com", "title": "A", "description": "desc A"},
                {"url": "https://b.com", "metadata": {"title": "B"}, "description": "desc B"},
            ]
        }
        provider = FirecrawlProvider.create(api_key="key")
        mock_client = _make_client_mock(_make_mock_response(200, response_body))

        with patch.object(provider, "_get_client", return_value=mock_client):
            results = await provider.search("test", limit=10)

        assert len(results) == 2
        assert isinstance(results[0], SearchHit)
        assert results[0].url == "https://a.com"
        assert results[0].snippet == "desc A"

    async def test_search_falls_back_to_results_key(self) -> None:
        response_body = {
            "results": [
                {"url": "https://c.com", "title": "C", "description": "desc C"},
            ]
        }
        provider = FirecrawlProvider.create(api_key="key")
        mock_client = _make_client_mock(_make_mock_response(200, response_body))

        with patch.object(provider, "_get_client", return_value=mock_client):
            results = await provider.search("test")

        assert len(results) == 1
        assert results[0].url == "https://c.com"

    async def test_search_http_error_raises(self) -> None:
        provider = FirecrawlProvider.create(api_key="key")
        mock_client = _make_client_mock(_make_mock_response(429, {"error": "Rate limited"}))

        with patch.object(provider, "_get_client", return_value=mock_client):
            with pytest.raises(SearchFailed, match="429"):
                await provider.search("query")

    async def test_search_network_error_raises(self) -> None:
        provider = FirecrawlProvider.create(api_key="key")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.TimeoutException("timeout", request=MagicMock())
        )

        with patch.object(provider, "_get_client", return_value=mock_client):
            with pytest.raises(SearchFailed):
                await provider.search("query")


class TestFirecrawlExtract:
    async def test_extract_parses_v1_scrape_format(self) -> None:
        response_body = {
            "data": {
                "metadata": {"title": "Page Title"},
                "markdown": "# Heading\n\nContent",
                "links": ["https://link.com"],
            }
        }
        provider = FirecrawlProvider.create(api_key="key")
        mock_client = _make_client_mock(_make_mock_response(200, response_body))

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.extract("https://example.com")

        assert isinstance(result, ExtractResult)
        assert result.title == "Page Title"
        assert "# Heading" in result.content
        assert result.links == ["https://link.com"]

    async def test_extract_sends_markdown_format(self) -> None:
        provider = FirecrawlProvider.create(api_key="key")
        mock_client = _make_client_mock(
            _make_mock_response(200, {"data": {"markdown": "", "metadata": {}}})
        )

        with patch.object(provider, "_get_client", return_value=mock_client):
            await provider.extract("https://example.com")

        call_kwargs = mock_client.post.call_args
        payload = call_kwargs[1].get("json", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else {})
        assert "markdown" in payload.get("formats", [])

    async def test_extract_http_error_raises(self) -> None:
        provider = FirecrawlProvider.create(api_key="key")
        mock_client = _make_client_mock(_make_mock_response(403, {"error": "Forbidden"}))

        with patch.object(provider, "_get_client", return_value=mock_client):
            with pytest.raises(ExtractFailed):
                await provider.extract("https://example.com")

    async def test_extract_network_error_raises(self) -> None:
        provider = FirecrawlProvider.create(api_key="key")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.ConnectError("DNS error", request=MagicMock())
        )

        with patch.object(provider, "_get_client", return_value=mock_client):
            with pytest.raises(ExtractFailed):
                await provider.extract("https://example.com")


class TestFirecrawlClientReuse:
    async def test_client_reuse_across_calls(self) -> None:
        """httpx.AsyncClient constructor must be called at most once (SPEC-018 Wave B1)."""
        provider = FirecrawlProvider.create(api_key="fc-key")
        mock_response = _make_mock_response(200, {"data": []})

        constructor_call_count = 0
        shared_client = AsyncMock()
        shared_client.post = AsyncMock(return_value=mock_response)
        shared_client.aclose = AsyncMock()

        def counting_constructor(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal constructor_call_count
            constructor_call_count += 1
            return shared_client

        with patch(
            "arcagent.modules.web.providers.firecrawl.httpx.AsyncClient", counting_constructor
        ):
            await provider.search("first")
            await provider.search("second")

        assert constructor_call_count == 1, (
            f"httpx.AsyncClient was constructed {constructor_call_count} times; expected 1"
        )

    async def test_close_releases_client(self) -> None:
        provider = FirecrawlProvider.create(api_key="fc-key")
        mock_response = _make_mock_response(200, {"data": []})
        shared_client = AsyncMock()
        shared_client.post = AsyncMock(return_value=mock_response)
        shared_client.aclose = AsyncMock()

        with patch(
            "arcagent.modules.web.providers.firecrawl.httpx.AsyncClient",
            return_value=shared_client,
        ):
            await provider.search("query")
            assert provider._client is not None
            await provider.close()
            assert provider._client is None
            shared_client.aclose.assert_called_once()
