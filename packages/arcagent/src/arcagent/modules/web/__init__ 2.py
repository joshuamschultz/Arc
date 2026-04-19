"""Web module — web search and extraction tools for ArcAgent.

Provides pluggable web search and content extraction with:
  - Provider-agnostic Protocols (WebSearchProvider, WebExtractProvider)
  - Adapters for Parallel.ai, Firecrawl, and Tavily
  - Federal URL allowlist enforcement (glob patterns)
  - PII redaction via arcllm.security at federal/enterprise tier
  - Content size cap with non-silent truncation

Public surface::

    from arcagent.modules.web import WebModule, WebConfig
    from arcagent.modules.web import WebSearchProvider, WebExtractProvider
    from arcagent.modules.web import SearchHit, ExtractResult
    from arcagent.modules.web import (
        WebError, SearchFailed, ExtractFailed,
        URLNotAllowed, ContentTooLarge, ProviderConfigMissing
    )

Spec: SPEC-018 T4.8
"""

from arcagent.modules.web.config import WebConfig
from arcagent.modules.web.errors import (
    ContentTooLarge,
    ExtractFailed,
    ProviderConfigMissing,
    SearchFailed,
    URLNotAllowed,
    WebError,
)
from arcagent.modules.web.protocols import (
    ExtractResult,
    SearchHit,
    WebExtractProvider,
    WebSearchProvider,
)
from arcagent.modules.web.web_module import WebModule

__all__ = [
    "ContentTooLarge",
    "ExtractFailed",
    "ExtractResult",
    "ProviderConfigMissing",
    "SearchFailed",
    "SearchHit",
    "URLNotAllowed",
    "WebConfig",
    "WebError",
    "WebExtractProvider",
    "WebModule",
    "WebSearchProvider",
]
