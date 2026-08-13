"""Provider adapters for the web module.

Each sub-module implements WebSearchProvider and/or WebExtractProvider
via duck-typing — no common base class is required.

Available adapters:
    browser    — keyless; reads pages through the browser module's CDP backend
    parallel   — Parallel.ai API (key required)
    firecrawl  — Firecrawl API (key required)
    tavily     — Tavily Search / Extract API (key required)

Spec: SPEC-018 T4.8.2
"""

from arcagent.modules.web.providers.browser_page import BrowserPageProvider
from arcagent.modules.web.providers.firecrawl import FirecrawlProvider
from arcagent.modules.web.providers.parallel import ParallelProvider
from arcagent.modules.web.providers.tavily import TavilyProvider

__all__ = [
    "BrowserPageProvider",
    "FirecrawlProvider",
    "ParallelProvider",
    "TavilyProvider",
]
