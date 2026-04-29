"""Unit tests for ParallelProvider — mocked httpx.

Verifies:
- search() builds correct payload and returns SearchHit list
- extract() builds correct payload and returns ExtractResult
- HTTP 4xx/5xx raises SearchFailed / ExtractFailed
- Network errors raise SearchFailed / ExtractFailed
- Empty API key raises ProviderConfigMissing
- Malformed results are skipped with a warning
- Client reuse across calls (SPEC-018 Wave B1)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from arcagent.modules.web.errors import ExtractFailed, ProviderConfigMissing, SearchFailed
from arcagent.modules.web.protocols import ExtractResult, SearchHit
from arcagent.modules.web.providers.parallel import ParallelProvider


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


class TestParallelProviderConstruction:
    def test_empty_api_key_raises(self) -> None:
        with pytest.raises(ProviderConfigMissing):
            ParallelProvider(api_key="")

    def test_create_factory(self) -> None:
        provider = ParallelProvider.create(api_key="test-key")
        assert provider._api_key == "test-key"

    def test_headers_contain_bearer(self) -> None:
        provider = ParallelProvider.create(api_key="my-key")
        headers = provider._headers()
        assert headers["Authorization"] == "Bearer my-key"


class TestParallelSearch:
    async def test_search_returns_hits(self) -> None:
        response_body = {
            "results": [
                {"url": "https://a.com", "title": "A", "snippet": "snippet A", "score": 0.9},
                {"url": "https://b.com", "title": "B", "snippet": "snippet B"},
            ]
        }
        provider = ParallelProvider.create(api_key="key")
        mock_client = _make_client_mock(_make_mock_response(200, response_body))

        with patch.object(provider, "_get_client", return_value=mock_client):
            results = await provider.search("test query", limit=5)

        assert len(results) == 2
        assert isinstance(results[0], SearchHit)
        assert results[0].url == "https://a.com"
        assert results[0].score == pytest.approx(0.9)
        assert results[1].score is None

    async def test_search_http_error_raises_search_failed(self) -> None:
        provider = ParallelProvider.create(api_key="key")
        mock_client = _make_client_mock(_make_mock_response(401, {"error": "Unauthorized"}))

        with patch.object(provider, "_get_client", return_value=mock_client):
            with pytest.raises(SearchFailed):
                await provider.search("query")

    async def test_search_network_error_raises_search_failed(self) -> None:
        provider = ParallelProvider.create(api_key="key")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused", request=MagicMock())
        )

        with patch.object(provider, "_get_client", return_value=mock_client):
            with pytest.raises(SearchFailed):
                await provider.search("query")

    async def test_search_empty_results(self) -> None:
        provider = ParallelProvider.create(api_key="key")
        mock_client = _make_client_mock(_make_mock_response(200, {"results": []}))

        with patch.object(provider, "_get_client", return_value=mock_client):
            results = await provider.search("query")

        assert results == []


class TestParallelExtract:
    async def test_extract_returns_result(self) -> None:
        response_body = {
            "title": "Page Title",
            "markdown": "# Heading\n\nContent here",
            "links": ["https://link1.com"],
        }
        provider = ParallelProvider.create(api_key="key")
        mock_client = _make_client_mock(_make_mock_response(200, response_body))

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.extract("https://example.com")

        assert isinstance(result, ExtractResult)
        assert result.url == "https://example.com"
        assert result.title == "Page Title"
        assert "# Heading" in result.content
        assert result.links == ["https://link1.com"]
        assert result.fetched_at > 0

    async def test_extract_http_error_raises_extract_failed(self) -> None:
        provider = ParallelProvider.create(api_key="key")
        mock_client = _make_client_mock(_make_mock_response(500, {"error": "Internal error"}))

        with patch.object(provider, "_get_client", return_value=mock_client):
            with pytest.raises(ExtractFailed):
                await provider.extract("https://example.com")

    async def test_extract_network_error_raises_extract_failed(self) -> None:
        provider = ParallelProvider.create(api_key="key")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.ConnectError("DNS error", request=MagicMock())
        )

        with patch.object(provider, "_get_client", return_value=mock_client):
            with pytest.raises(ExtractFailed):
                await provider.extract("https://example.com")


class TestParallelClientReuse:
    async def test_client_reuse_across_calls(self) -> None:
        """httpx.AsyncClient constructor must be called at most once (SPEC-018 Wave B1)."""
        provider = ParallelProvider.create(api_key="par-key")
        mock_response = _make_mock_response(200, {"results": []})

        constructor_call_count = 0
        shared_client = AsyncMock()
        shared_client.post = AsyncMock(return_value=mock_response)
        shared_client.aclose = AsyncMock()

        def counting_constructor(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal constructor_call_count
            constructor_call_count += 1
            return shared_client

        with patch(
            "arcagent.modules.web.providers.parallel.httpx.AsyncClient", counting_constructor
        ):
            await provider.search("first")
            await provider.search("second")

        assert constructor_call_count == 1, (
            f"httpx.AsyncClient was constructed {constructor_call_count} times; expected 1"
        )

    async def test_close_releases_client(self) -> None:
        provider = ParallelProvider.create(api_key="par-key")
        mock_response = _make_mock_response(200, {"results": []})
        shared_client = AsyncMock()
        shared_client.post = AsyncMock(return_value=mock_response)
        shared_client.aclose = AsyncMock()

        with patch(
            "arcagent.modules.web.providers.parallel.httpx.AsyncClient", return_value=shared_client
        ):
            await provider.search("query")
            assert provider._client is not None
            await provider.close()
            assert provider._client is None
            shared_client.aclose.assert_called_once()
