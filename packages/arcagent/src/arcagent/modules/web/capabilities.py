"""Decorator-form web module — SPEC-021 capability surface.

Two ``@tool`` functions that mirror the legacy :class:`WebModule` surface:

  * ``@tool web_search(query, limit=10)`` — PII-redacted search, returns
    a JSON array of result objects (url, title, snippet, score).
  * ``@tool web_extract(url)`` — URL-policy-checked content extraction,
    returns a JSON object (url, title, content, links, fetched_at).

State is shared via :mod:`arcagent.modules.web._runtime`. The agent
configures it once at startup; tools read state lazily on each invocation.

The legacy :class:`WebModule` class continues to exist alongside this
module; both forms route to the same provider adapters and audit semantics.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from arcagent.modules.web import _runtime
from arcagent.modules.web.errors import ExtractFailed, SearchFailed, URLNotAllowed
from arcagent.modules.web.url_policy import is_url_allowed
from arcagent.modules.web.web_module import (
    _hash_query,
    _redact_if_enabled,
    _truncate_content,
)
from arcagent.tools._decorator import tool
from arcagent.utils.audit import safe_audit

_logger = logging.getLogger("arcagent.modules.web.capabilities")

# Bound error message length in audit payloads to prevent log inflation (LLM02).
_MAX_ERROR_MSG_LEN: int = 200


@tool(
    name="web_search",
    description=(
        "Search the web and return ranked results. "
        "Returns a JSON array of objects with url, title, snippet, and score fields. "
        "Queries are PII-redacted at federal/enterprise tiers before being sent to "
        "the provider."
    ),
    classification="read_only",
    capability_tags=["web", "search", "read_only"],
    when_to_use=(
        "When you need to find information on the web, research a topic, or "
        "discover relevant URLs for further extraction."
    ),
    version="1.0.0",
)
async def web_search(query: str, limit: int = 10) -> str:
    """Execute a web search. Returns a JSON string for tool transport."""
    results = await _search(query, limit=limit)
    return json.dumps(results)


@tool(
    name="web_extract",
    description=(
        "Extract the full Markdown content of a web page from a URL. "
        "Returns a JSON object with url, title, content, links, and fetched_at fields. "
        "URL allowlist is enforced before any network request; content is truncated "
        "at the configured size cap and PII-redacted at federal/enterprise tiers."
    ),
    classification="read_only",
    capability_tags=["web", "extract", "read_only"],
    when_to_use=(
        "When you need to read the full content of a specific web page, "
        "fetch documentation, or extract structured information from a known URL."
    ),
    version="1.0.0",
)
async def web_extract(url: str) -> str:
    """Extract web content from ``url``. Returns a JSON string for tool transport."""
    result = await _extract(url)
    return json.dumps(result)


# --- Internal pipelines (shared by both tools) --------------------------------


async def _search(query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Run the full search pipeline with PII redaction and audit."""
    st = _runtime.state()
    cfg = st.config

    pii_on = cfg.pii_redaction_enabled or cfg.tier == "federal"
    redacted_query = _redact_if_enabled(query, enabled=pii_on)
    query_hash = _hash_query(redacted_query)

    if st.search_provider is None:
        raise SearchFailed("web_search called before a search provider was configured")

    try:
        hits = await st.search_provider.search(redacted_query, limit=limit)
    except SearchFailed:
        raise
    except Exception as exc:
        raise SearchFailed(
            f"Unexpected error during web search: {type(exc).__name__}",
        ) from exc

    await safe_audit(
        st.telemetry,
        "web.search",
        {
            "provider": cfg.search_provider,
            "query_hash": query_hash,
            "result_count": len(hits),
        },
        logger=_logger,
    )

    return [h.model_dump() for h in hits]


async def _extract(url: str) -> dict[str, Any]:
    """Run the full extraction pipeline with URL policy, size cap, and audit."""
    st = _runtime.state()
    cfg = st.config

    # Enforce URL allowlist BEFORE any network request (ASI02, LLM06).
    if not is_url_allowed(url, allowlist=cfg.url_allowlist, tier=cfg.tier):
        await safe_audit(
            st.telemetry,
            "web.url_denied",
            {"url": url, "tier": cfg.tier},
            logger=_logger,
        )
        raise URLNotAllowed(url=url, tier=cfg.tier)

    if st.extract_provider is None:
        raise ExtractFailed("web_extract called before an extract provider was configured")

    try:
        result = await st.extract_provider.extract(url)
    except ExtractFailed:
        raise
    except Exception as exc:
        raise ExtractFailed(
            f"Unexpected error during web extraction: {type(exc).__name__}",
        ) from exc

    # Apply content size cap — truncate, never silently drop.
    content = _truncate_content(result.content, cfg.max_content_bytes, url)

    # Apply PII redaction on extracted content.
    pii_on = cfg.pii_redaction_enabled or cfg.tier == "federal"
    content = _redact_if_enabled(content, enabled=pii_on)

    final = result.model_copy(update={"content": content})

    await safe_audit(
        st.telemetry,
        "web.extract",
        {
            "provider": cfg.extract_provider,
            "url": url,
            "content_size_bytes": len(content.encode("utf-8")),
            "allowlist_match": True,
        },
        logger=_logger,
    )

    return final.model_dump()
