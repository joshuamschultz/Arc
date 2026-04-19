"""Provider adapters for the web module.

Each sub-module implements WebSearchProvider and/or WebExtractProvider
via duck-typing — no common base class is required.

Available adapters:
    parallel   — Parallel.ai API
    firecrawl  — Firecrawl API
    tavily     — Tavily Search / Extract API

Spec: SPEC-018 T4.8.2
"""

from arcagent.modules.web.providers.firecrawl import FirecrawlProvider
from arcagent.modules.web.providers.parallel import ParallelProvider
from arcagent.modules.web.providers.tavily import TavilyProvider

__all__ = [
    "FirecrawlProvider",
    "ParallelProvider",
    "TavilyProvider",
]
