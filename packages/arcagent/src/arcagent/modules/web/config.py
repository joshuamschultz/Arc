"""Configuration for the web module.

WebConfig is loaded from ``[modules.web]`` in arcagent.toml and validated
by Pydantic on construction.  All fields have conservative defaults so the
module works out-of-the-box at personal tier.

Tier-specific behaviour:
    Federal  — ``url_allowlist`` REQUIRED (non-empty); deny-by-default.
               Any URL not matching the allowlist raises URLNotAllowed.
               PII redaction is mandatory and cannot be disabled.
    Enterprise — ``url_allowlist`` optional; empty = allow all.
                 PII redaction off by default (opt-in).
    Personal — Allow-by-default (empty allowlist = allow all); PII redaction
               off by default (opt-in).

Spec: SPEC-018 T4.8.5
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from arcagent.core.module_config import ModuleConfig


class WebConfig(ModuleConfig):
    """Root configuration for the web module.

    Loaded from::

        [modules.web]
        search_provider = "tavily"
        extract_provider = "firecrawl"
        tier = "personal"
        url_allowlist = []
        max_content_bytes = 1_000_000
        pii_redaction_enabled = false
    """

    # Provider selection
    search_provider: Literal["parallel", "firecrawl", "tavily"] = "tavily"
    extract_provider: Literal["parallel", "firecrawl", "tavily"] = "firecrawl"

    # Deployment tier — drives allowlist enforcement and PII defaults
    tier: str = "personal"

    # Glob patterns; empty = allow all (personal/enterprise only).
    # Federal tier enforces non-empty at module startup, not here,
    # because config validation happens before tier context is fully resolved.
    url_allowlist: list[str] = Field(default_factory=list)

    # Content size cap; extracted content is truncated at this byte limit.
    # A ContentTooLarge warning is logged but truncated content is returned.
    max_content_bytes: int = Field(default=1_000_000, ge=1_024)

    # PII redaction — mandatory at federal (forced on at the call site
    # regardless of this flag), OFF by default at personal/enterprise. Opt in
    # per deployment; setting True is honored at every tier.
    pii_redaction_enabled: bool = False

    # HTTP timeout for provider requests (seconds)
    request_timeout_s: float = Field(default=30.0, ge=1.0, le=120.0)


__all__ = ["WebConfig"]
