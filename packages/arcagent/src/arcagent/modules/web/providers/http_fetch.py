"""Keyless HTTP page extractor — fetch a URL and reduce it to text, no browser.

The default ``web_extract`` path has to work on a headless box with no Chrome and
no account. The CDP extractor could not: with no ``cdp_url`` it launches a local
Chrome, and a bare server has none, so every lookup failed at the launch. This
provider is a plain ``httpx`` GET plus a stdlib HTML-to-text reduction — no Chrome
binary, no API key, no launch to fail.

It does not run JavaScript. A page that renders its body client-side returns
little here; for those an agent uses the ``browser_*`` tools. But for the common
lookup — read what a page says — this is the reliable path, and it reuses the
same allowlist / size-cap / PII pipeline in ``web.capabilities`` that every other
provider's result flows through.
"""

from __future__ import annotations

import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from arcagent.modules.web.protocols import ExtractResult

#: A neutral, honest agent string — never impersonating a browser to defeat a block.
_USER_AGENT = "ArcWebExtract/1.0 (+https://arc.local)"

#: Tags whose text is markup/behavior, never page content. Not ``head`` — the
#: title lives there and is wanted; head's other children carry no visible text.
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})

#: Tags that introduce a line break when reducing to text.
_BREAK_TAGS = frozenset(
    {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
)


class _Reader(HTMLParser):
    """Collect a page's title, visible text, and absolute-resolvable links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links: list[str] = []
        self._chunks: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in _BREAK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title += data
        else:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r"\n[ ]+", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


class HttpFetchProvider:
    """A ``WebExtractProvider`` that fetches over HTTP and never drives a browser."""

    def __init__(
        self, *, timeout_s: float = 20.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._timeout = timeout_s
        self._transport = transport  # test seam only; None uses the real network

    @classmethod
    def create(cls, *, timeout_s: float = 20.0) -> HttpFetchProvider:
        return cls(timeout_s=timeout_s)

    async def extract(self, url: str) -> ExtractResult:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=self._timeout,
            headers={"User-Agent": _USER_AGENT},
            transport=self._transport,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            html = response.text
            final_url = str(response.url)

        reader = _Reader()
        reader.feed(html)

        links: list[str] = []
        seen: set[str] = set()
        for href in reader.links:
            absolute = urljoin(final_url, href)
            if absolute.startswith(("http://", "https://")) and absolute not in seen:
                seen.add(absolute)
                links.append(absolute)

        return ExtractResult(
            url=final_url,
            title=reader.title.strip(),
            content=reader.text(),
            links=links,
            fetched_at=time.time(),
        )


__all__ = ["HttpFetchProvider"]
