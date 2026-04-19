"""Unit tests for arcskill.hub.sources — all network calls mocked via httpx.MockTransport.

Coverage targets (HIGH severity gap):
- GitHubSource.fetch: release API + asset download
- RegistrySource.fetch: index.json + bundle download
- WellKnownSource.fetch: discovery endpoint + bundle download
- 50MB size cap enforced during streaming
- HTTPS-only: http:// URLs rejected
- sha256 computed correctly on downloaded bytes
- Network timeout/HTTP errors surfaced as clean errors
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from arcskill.hub.config import SkillSource
from arcskill.hub.sources import (
    _MAX_BUNDLE_BYTES,
    FetchResult,
    GitHubSource,
    RegistrySource,
    WellKnownSource,
    _sha256_file,
    _stream_download,
    make_adapter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bundle_bytes(content: str = "fake-tar-gz-content") -> bytes:
    return content.encode()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _github_source(repo: str = "arc-foundation/skills") -> SkillSource:
    return SkillSource(name="arc-official", type="github", repo=repo)


def _registry_source(url: str = "https://skills.example.com/v1") -> SkillSource:
    return SkillSource(name="arc-registry", type="registry", url=url)


def _wellknown_source(url: str = "https://example.com") -> SkillSource:
    return SkillSource(name="arc-wk", type="wellknown", url=url)


@contextmanager
def _mock_httpx_client(handler):
    """Context manager that replaces httpx.Client with a subclass that uses MockTransport.

    The subclass IS a real httpx.Client so isinstance() checks in source code pass.
    Each construction creates a fresh client with the mock transport injected.
    """
    transport = httpx.MockTransport(handler)

    class _MockedClient(httpx.Client):
        """httpx.Client subclass that ignores the caller's transport and uses the mock."""

        def __init__(self, *args, **kwargs):
            kwargs.pop("transport", None)
            super().__init__(*args, transport=transport, **kwargs)

    with patch("httpx.Client", new=_MockedClient):
        yield


# ---------------------------------------------------------------------------
# GitHubSource
# ---------------------------------------------------------------------------


class TestGitHubSourceFetch:
    def test_fetch_uses_release_api_then_downloads_asset(self, tmp_path: Path) -> None:
        """GitHubSource calls release API, finds matching asset, downloads it."""
        bundle = _make_bundle_bytes("github-bundle")
        release_json = {
            "tag_name": "v1.2.3",
            "assets": [
                {
                    "name": "summarise.tar.gz",
                    "browser_download_url": (
                        "https://github.com/arc-foundation/skills"
                        "/releases/download/v1.2.3/summarise.tar.gz"
                    ),
                }
            ],
        }

        def _handler(request: httpx.Request) -> httpx.Response:
            if "releases/latest" in str(request.url):
                return httpx.Response(200, json=release_json)
            return httpx.Response(200, content=bundle)

        adapter = GitHubSource(_github_source())
        with _mock_httpx_client(_handler):
            result = adapter.fetch("summarise", tmp_path)

        assert isinstance(result, FetchResult)
        assert result.version == "v1.2.3"
        assert result.local_path.exists()
        assert result.content_hash == _sha256_bytes(bundle)
        assert result.source_name == "arc-official"

    def test_fetch_constructs_fallback_url_when_no_matching_asset(
        self, tmp_path: Path
    ) -> None:
        """When asset list has no match, URL is constructed from version + name."""
        bundle = _make_bundle_bytes("fallback-bundle")
        release_json = {"tag_name": "v2.0.0", "assets": []}

        def _handler(request: httpx.Request) -> httpx.Response:
            if "releases/latest" in str(request.url):
                return httpx.Response(200, json=release_json)
            return httpx.Response(200, content=bundle)

        adapter = GitHubSource(_github_source())
        with _mock_httpx_client(_handler):
            result = adapter.fetch("summarise", tmp_path)

        assert result.version == "v2.0.0"
        assert "summarise" in result.bundle_url

    def test_fetch_no_repo_raises_value_error(self, tmp_path: Path) -> None:
        source = SkillSource(name="bad", type="github")  # repo is None
        adapter = GitHubSource(source)
        with pytest.raises(ValueError, match="repo"):
            adapter.fetch("any", tmp_path)

    def test_fetch_api_404_raises_http_status_error(self, tmp_path: Path) -> None:
        """Non-2xx release API response raises HTTPStatusError."""

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="not found")

        adapter = GitHubSource(_github_source())
        with _mock_httpx_client(_handler):
            with pytest.raises(httpx.HTTPStatusError):
                adapter.fetch("summarise", tmp_path)


# ---------------------------------------------------------------------------
# RegistrySource
# ---------------------------------------------------------------------------


class TestRegistrySourceFetch:
    def test_fetch_reads_index_then_downloads_bundle(self, tmp_path: Path) -> None:
        """RegistrySource GETs index.json then downloads the skill bundle."""
        bundle = _make_bundle_bytes("registry-bundle")
        bundle_url = "https://cdn.example.com/bundles/summarise.tar.gz"
        index = {
            "skills": {
                "summarise": {"version": "0.9.1", "url": bundle_url}
            }
        }

        def _handler(request: httpx.Request) -> httpx.Response:
            if "index.json" in str(request.url):
                return httpx.Response(200, json=index)
            return httpx.Response(200, content=bundle)

        adapter = RegistrySource(_registry_source())
        with _mock_httpx_client(_handler):
            result = adapter.fetch("summarise", tmp_path)

        assert result.version == "0.9.1"
        assert result.bundle_url == bundle_url
        assert result.content_hash == _sha256_bytes(bundle)

    def test_fetch_skill_not_in_index_raises_runtime_error(
        self, tmp_path: Path
    ) -> None:
        index = {"skills": {}}  # empty — skill not listed

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=index)

        adapter = RegistrySource(_registry_source())
        with _mock_httpx_client(_handler):
            with pytest.raises(RuntimeError, match="not found"):
                adapter.fetch("missing-skill", tmp_path)

    def test_fetch_no_url_raises_value_error(self, tmp_path: Path) -> None:
        source = SkillSource(name="bad", type="registry")  # url is None
        adapter = RegistrySource(source)
        with pytest.raises(ValueError, match="url"):
            adapter.fetch("any", tmp_path)


# ---------------------------------------------------------------------------
# WellKnownSource
# ---------------------------------------------------------------------------


class TestWellKnownSourceFetch:
    def test_fetch_discovers_then_downloads_bundle(self, tmp_path: Path) -> None:
        """WellKnownSource fetches /.well-known/skills/index.json then downloads."""
        bundle = _make_bundle_bytes("wellknown-bundle")
        bundle_url = "https://example.com/skills/summarise.tar.gz"
        index = {
            "skills": {
                "summarise": {"version": "1.0.0", "url": bundle_url}
            }
        }

        def _handler(request: httpx.Request) -> httpx.Response:
            if ".well-known/skills/index.json" in str(request.url):
                return httpx.Response(200, json=index)
            return httpx.Response(200, content=bundle)

        adapter = WellKnownSource(_wellknown_source())
        with _mock_httpx_client(_handler):
            result = adapter.fetch("summarise", tmp_path)

        assert result.version == "1.0.0"
        assert result.bundle_url == bundle_url
        assert result.content_hash == _sha256_bytes(bundle)

    def test_fetch_skill_not_in_index_raises_runtime_error(
        self, tmp_path: Path
    ) -> None:
        index = {"skills": {}}

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=index)

        adapter = WellKnownSource(_wellknown_source())
        with _mock_httpx_client(_handler):
            with pytest.raises(RuntimeError, match="not found"):
                adapter.fetch("missing", tmp_path)

    def test_fetch_no_url_raises_value_error(self, tmp_path: Path) -> None:
        source = SkillSource(name="bad", type="wellknown")  # url is None
        adapter = WellKnownSource(source)
        with pytest.raises(ValueError, match="url"):
            adapter.fetch("any", tmp_path)


# ---------------------------------------------------------------------------
# 50 MB size cap enforcement
# ---------------------------------------------------------------------------


class TestSizeCap:
    def test_stream_download_raises_when_exceeding_cap(self, tmp_path: Path) -> None:
        """A response that exceeds _MAX_BUNDLE_BYTES raises RuntimeError."""
        over_cap = _MAX_BUNDLE_BYTES + 1

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * over_cap)

        transport = httpx.MockTransport(_handler)
        dest = tmp_path / "big.tar.gz"

        with httpx.Client(transport=transport) as client:
            with pytest.raises(RuntimeError, match="exceeded"):
                _stream_download(client, "https://example.com/big.tar.gz", dest)

    def test_stream_download_accepts_exactly_cap(self, tmp_path: Path) -> None:
        """Response exactly at _MAX_BUNDLE_BYTES is accepted (strict > check)."""
        exactly_cap = _MAX_BUNDLE_BYTES

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"y" * exactly_cap)

        transport = httpx.MockTransport(_handler)
        dest = tmp_path / "cap.tar.gz"

        with httpx.Client(transport=transport) as client:
            _stream_download(client, "https://example.com/cap.tar.gz", dest)

        assert dest.exists()
        assert dest.stat().st_size == exactly_cap


# ---------------------------------------------------------------------------
# HTTPS-only enforcement
# ---------------------------------------------------------------------------


class TestHttpsOnly:
    def test_http_url_rejected_by_stream_download(self, tmp_path: Path) -> None:
        """_stream_download raises ValueError for http:// URLs."""

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"data")

        transport = httpx.MockTransport(_handler)
        dest = tmp_path / "out.tar.gz"

        with httpx.Client(transport=transport) as client:
            with pytest.raises(ValueError, match="HTTPS"):
                _stream_download(client, "http://evil.example.com/bundle.tar.gz", dest)

    def test_https_url_accepted(self, tmp_path: Path) -> None:
        """HTTPS URLs proceed past the security check."""

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"safe")

        transport = httpx.MockTransport(_handler)
        dest = tmp_path / "out.tar.gz"

        with httpx.Client(transport=transport) as client:
            _stream_download(client, "https://example.com/bundle.tar.gz", dest)

        assert dest.read_bytes() == b"safe"


# ---------------------------------------------------------------------------
# sha256 computation
# ---------------------------------------------------------------------------


class TestSha256:
    def test_sha256_file_matches_expected(self, tmp_path: Path) -> None:
        data = b"hello world"
        f = tmp_path / "test.bin"
        f.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        assert _sha256_file(f) == expected

    def test_sha256_file_on_large_file(self, tmp_path: Path) -> None:
        """Verify sha256 works on files larger than the 65536 read chunk."""
        data = b"a" * 200_000
        f = tmp_path / "large.bin"
        f.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        assert _sha256_file(f) == expected


# ---------------------------------------------------------------------------
# make_adapter factory
# ---------------------------------------------------------------------------


class TestMakeAdapter:
    def test_github_type_returns_github_source(self) -> None:
        source = SkillSource(name="x", type="github", repo="owner/repo")
        assert isinstance(make_adapter(source), GitHubSource)

    def test_registry_type_returns_registry_source(self) -> None:
        source = SkillSource(name="x", type="registry", url="https://example.com")
        assert isinstance(make_adapter(source), RegistrySource)

    def test_wellknown_type_returns_wellknown_source(self) -> None:
        source = SkillSource(name="x", type="wellknown", url="https://example.com")
        assert isinstance(make_adapter(source), WellKnownSource)
