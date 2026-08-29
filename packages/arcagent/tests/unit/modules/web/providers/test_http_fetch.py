"""The keyless httpx extractor: fetch a page, reduce it to text + links.

This is the default ``web_extract`` provider — it must work with no Chrome and no
API key, which is exactly why it replaced the CDP launch on headless boxes.
"""

from __future__ import annotations

import httpx
import pytest

from arcagent.modules.web.providers.http_fetch import HttpFetchProvider

_PAGE = """
<html>
  <head>
    <title>  ScienceLogic FedRAMP  </title>
    <style>.x { color: red }</style>
    <script>var tracked = 1;</script>
  </head>
  <body>
    <h1>Status</h1>
    <p>ScienceLogic SL1 holds a FedRAMP authorization.</p>
    <p>See the <a href="/marketplace">marketplace listing</a> and
       <a href="https://example.com/other">another page</a>.</p>
  </body>
</html>
"""


def _provider(handler) -> HttpFetchProvider:  # type: ignore[no-untyped-def]
    return HttpFetchProvider(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_extract_returns_title_text_and_absolute_links() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("ArcWebExtract")
        return httpx.Response(200, text=_PAGE)

    result = await _provider(handler).extract("https://sciencelogic.example/status")

    assert result.title == "ScienceLogic FedRAMP"
    assert "FedRAMP authorization" in result.content
    # script/style bodies are not content
    assert "var tracked" not in result.content and "color: red" not in result.content
    # relative link resolved to absolute against the fetched URL; both kept
    assert "https://sciencelogic.example/marketplace" in result.links
    assert "https://example.com/other" in result.links


@pytest.mark.asyncio
async def test_extract_follows_redirects_and_reports_the_final_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, text="<title>done</title><p>landed</p>")

    result = await _provider(handler).extract("https://host.example/start")

    assert result.url == "https://host.example/final"
    assert "landed" in result.content


@pytest.mark.asyncio
async def test_extract_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope")

    with pytest.raises(httpx.HTTPStatusError):
        await _provider(handler).extract("https://host.example/missing")
