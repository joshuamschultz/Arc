"""Secret-shaped content scanning for tool-write payloads.

Live incident (task #21): a user pasted a Browserbase API token into chat
and the agent wrote it verbatim to ``workspace/secrets/browserbase.md``.
Doctrine (``packages/arcagent/CLAUDE.md``): credentials never touch the
filesystem.

Detection is two layers:

1. arcllm's structured-prefix secret patterns (AWS/GitHub/JWT/PEM/DB URL/
   Anthropic/OpenAI/Google/Slack — ADR-423), reused read-only. arcagent
   already depends on arcllm, so this is a plain import, not a copy.
2. One arcagent-local, keyword-anchored heuristic for the class arcllm's
   patterns cannot see: a generic, unprefixed token pasted next to its own
   label (``browserbase_api_key: bb_live_...``) or an HTTP bearer token.
   This is exactly the live incident's shape — arcllm's SECRET_PATTERNS
   deliberately excludes bare Shannon-entropy detection (ADR-423) because a
   Browserbase-style key has no recognizable prefix. Requiring a keyword
   label keeps the false-positive rate low without reopening that
   deliberately-scoped-out entropy scan.

This module does NOT attempt general PII detection — that stays in
arcllm's ``RegexPiiDetector``. It answers one narrow question: does this
write payload look like a live credential that must never touch disk?
"""

from __future__ import annotations

import logging
import re
from typing import Any

from arcllm._secrets import SECRET_PATTERNS

from arcagent.core.errors import ToolError

_logger = logging.getLogger("arcagent.tools.secret_guard")

# Keyword + assignment operator, OR the HTTP "Bearer <token>" shape. Both
# require a run of 16+ token characters so short placeholders
# ("api_key = 'changeme'") don't trip it. The keyword boundary is a
# lookbehind/lookahead on LETTERS ONLY (not "\b") — "_" is a word
# character, so a plain "\b" would miss the common
# "browserbase_api_key" naming convention while still (correctly)
# refusing to match inside an unrelated word like "secretary".
_GENERIC_TOKEN_RE = re.compile(
    r"(?i)"
    r"(?:"
    r"(?<![a-z])(?:api[_-]?key|api[_-]?token|access[_-]?token|secret|password|client[_-]?secret)"
    r"(?![a-z])\s*[:=]\s*['\"]?[A-Za-z0-9_\-.]{16,}['\"]?"
    r"|"
    r"\bbearer\s+[A-Za-z0-9_\-.]{16,}\b"
    r")"
)


def find_secret(content: str) -> str | None:
    """Return a label for the first secret-shaped match in ``content``, else None."""
    for secret_type, pattern in SECRET_PATTERNS:
        if pattern.search(content):
            return secret_type
    if _GENERIC_TOKEN_RE.search(content):
        return "GENERIC_API_TOKEN"
    return None


def enforce_no_secret_content(
    content: str,
    *,
    tool_name: str,
    file_path: str,
    caller_did: str = "did:arc:unknown",
    audit_sink: Any = None,
) -> None:
    """Deny + audit a write whose payload looks like a live credential.

    Raises :class:`ToolError` (``TOOL_SECRET_WRITE_DENIED``) and emits a
    ``tool.secret_write.denied`` audit event carrying the tool name, caller
    DID, target path, and matched secret type. No-op when the content has
    no secret-shaped match.
    """
    secret_type = find_secret(content)
    if secret_type is None:
        return
    if audit_sink is not None:
        try:
            audit_sink(
                "tool.secret_write.denied",
                {
                    "tool": tool_name,
                    "actor_did": caller_did,
                    "path": file_path,
                    "secret_type": secret_type,
                },
            )
        except Exception:  # reason: fail-open — audit must not mask the denial
            _logger.exception("Secret-write audit sink raised; continuing")
    raise ToolError(
        code="TOOL_SECRET_WRITE_DENIED",
        message=(
            f"Refusing to write '{file_path}': content looks like a live credential "
            f"({secret_type}). Credentials never touch the filesystem — call "
            "store_secret(name=...) instead; it tells you exactly where the "
            "operator should place it."
        ),
        details={"path": file_path, "tool": tool_name, "secret_type": secret_type},
    )


__all__ = ["enforce_no_secret_content", "find_secret"]
