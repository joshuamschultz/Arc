"""Per-agent messaging module runtime context.

The messaging module's hooks, tools, and background polling task share
state (services, config, unread-count cache, agent chat callback, etc.).
Decorator-stamped functions can't carry that state in a closure, so it
lives in a module-level :class:`_State` instance configured by the agent
at startup.

Mirrors the pattern in :mod:`arcagent.modules.policy._runtime` and
:mod:`arcagent.modules.memory._runtime`. Single-agent-per-process is the
assumption; this is shared mutable state for one agent.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcagent.modules.messaging.config import MessagingConfig

_logger = logging.getLogger("arcagent.modules.messaging._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across messaging hooks, tools, and poll task."""

    config: MessagingConfig
    workspace: Path
    telemetry: Any
    team_root: Path
    agent_name: str
    # arcteam service objects — set by configure(), typed as Any to avoid
    # a hard import-time dependency on the optional arcteam package.
    svc: Any  # MessagingService
    registry: Any  # EntityRegistry
    # Latest unread counts per stream — updated by the poll loop and read
    # by the assemble_prompt hook for context injection.
    last_unread: dict[str, int] = field(default_factory=dict)
    # agent.chat() callback — bound via agent:ready event.
    agent_chat_fn: Any = None
    # Serialises message processing so only one inbox batch is in-flight.
    processing_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # TTL-cached team roster string; invalidated after roster_ttl_seconds.
    roster_cache: str | None = None
    roster_cache_time: float = 0.0


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    team_root: Path | None = None,
    agent_name: str = "",
) -> None:
    """Bind module state and bootstrap arcteam services.

    Called once at agent startup. Imports arcteam lazily so the module
    can be imported without arcteam installed (it is an optional dep).
    """
    global _state

    from arcteam.audit import AuditLogger
    from arcteam.messenger import MessagingService
    from arcteam.registry import EntityRegistry
    from arcteam.storage import FileBackend

    cfg = MessagingConfig(**(config or {}))
    ws = workspace.resolve()
    resolved_team_root = (team_root or (ws.parent / "team")).resolve()

    backend = FileBackend(resolved_team_root)
    audit = AuditLogger(
        backend,
        hmac_key=cfg.audit_hmac_key.encode("utf-8"),
    )
    # AuditLogger.initialize() is async; callers that need it initialised
    # before the first poll must await it separately (the poll loop waits
    # 1 s before its first cycle, giving startup time to complete).
    registry = EntityRegistry(backend, audit)
    svc = MessagingService(backend, registry, audit)

    _state = _State(
        config=cfg,
        workspace=ws,
        telemetry=telemetry,
        team_root=resolved_team_root,
        agent_name=agent_name,
        svc=svc,
        registry=registry,
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "messaging module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = ["configure", "reset", "state"]
