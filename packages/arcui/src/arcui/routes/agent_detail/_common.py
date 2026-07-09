"""Shared helpers and constants for the Agent Detail route subpackage.

Workspace resolution, the caller-DID label used in audit events, the
section whitelist for the config endpoint, and the small regex set
shared by the skills and tools modules.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from starlette.requests import Request

logger = logging.getLogger("arcui.routes.agent_detail")

# Whitelisted top-level config sections — anything else (e.g. ``[secrets]``,
# ``[identity.private_key]``) is dropped before serialization. Keep this list
# tight; it is the security boundary for LLM07 (system prompt leakage).
_CONFIG_WHITELIST: tuple[str, ...] = (
    "agent",
    "llm",
    "context",
    "session",
    "telemetry",
    "tools",
    # SPEC-022 Policy tab needs eval_interval_turns / max_bullets / etc. These
    # sections never carry secrets — `modules.<name>.config` is wiring data,
    # `eval` is the reflection model config (model name, fallback, timeout).
    "modules",
    "eval",
    "extensions",
    "team",
    "vault",
    "identity",
)

# Caller DID used in audit events. arcui has no per-user DID today; this is
# the gateway-side actor for "ui requested this read."
_CALLER_DID = "did:arc:ui:viewer"

# Session id format: alphanumeric / dash / underscore / dot only. Defends
# against ``../`` injection on session replay (path component, not query).
_VALID_SID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Roots accepted by /files/* endpoints. ``workspace`` resolves to
# ``team/<agent>/workspace/``; ``agent`` to ``team/<agent>/``.
_VALID_ROOTS = frozenset({"workspace", "agent"})

# Frontmatter delimiter for skill files — three dashes on a line by themselves.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _agent_root(request: Request, agent_id: str) -> Path | None:
    """Look up an agent's filesystem root via the injected roster provider.

    Returns the absolute path to ``team/<dir>_agent/`` (the agent root, NOT
    the workspace subdir). Callers select the workspace root explicitly via
    the ``root`` query param.
    """
    provider = getattr(request.app.state, "roster_provider", None)
    if provider is None:
        return None
    for entry in provider():
        if entry.agent_id == agent_id:
            return Path(entry.workspace_path)
    return None


def _agent_did(request: Request, agent_id: str) -> str | None:
    """Resolve an agent's DID (audit actor) from the injected roster provider.

    The audit chain filters on ``actor_did`` (a DID), but the agent-detail
    routes key on the human agent label; this bridges label -> DID so the
    per-agent audit tab reads the durable chain instead of nothing.
    """
    provider = getattr(request.app.state, "roster_provider", None)
    if provider is None:
        return None
    for entry in provider():
        if entry.agent_id == agent_id:
            did: str = entry.did
            return did
    return None


def _resolve_root_path(agent_root: Path, root_arg: str) -> Path:
    """Map ``root_arg`` to the resolved filesystem path."""
    if root_arg == "workspace":
        return agent_root / "workspace"
    return agent_root
