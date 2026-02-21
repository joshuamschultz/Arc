"""Messaging module — inter-agent communication via ArcTeam.

Integrates the arcteam messaging subsystem as an arcagent module.
Provides tools for the LLM, background inbox polling, and context
injection of unread message summaries into the system prompt.

Zero hardcoded coupling to arcagent core — this is a pure module.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from arcagent.core.module_bus import EventContext, ModuleContext
from arcagent.core.telemetry import AgentTelemetry
from arcagent.modules.messaging.config import MessagingConfig

_logger = logging.getLogger("arcagent.messaging")


class MessagingModule:
    """Inter-agent messaging module — Module Bus participant.

    On startup:
    1. Bootstraps arcteam services (FileBackend, AuditLogger, Registry, Messenger)
    2. Registers this agent as an entity in the team registry
    3. Registers 5 LLM-callable tools (send, inbox, thread, entities, channels)
    4. Subscribes to agent:assemble_prompt to inject unread message counts
    5. Starts a background polling loop that emits messaging:new_messages events
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        team_config: Any | None = None,
        telemetry: AgentTelemetry | None = None,
        workspace: Path = Path("."),
    ) -> None:
        self._config = MessagingConfig(**(config or {}))
        self._team_config = team_config
        self._telemetry = telemetry
        self._workspace = workspace
        self._poll_task: asyncio.Task[None] | None = None
        self._svc: Any = None  # MessagingService — set during startup
        self._registry: Any = None  # EntityRegistry — set during startup
        self._last_unread: dict[str, int] = {}  # stream -> unread count cache

    def _resolve_team_root(self) -> Path:
        """Resolve team root directory.

        Priority:
        1. ``[team] root`` from agent config (injected as team_config)
        2. ``workspace / "team"`` fallback

        Relative paths are resolved against workspace.
        """
        root_str = ""
        if self._team_config is not None:
            root_str = getattr(self._team_config, "root", "")

        if root_str:
            team_root = Path(root_str)
        else:
            team_root = Path("team")

        if not team_root.is_absolute():
            # Resolve relative to agent directory (workspace parent), not workspace.
            # [team] root = "../shared" is relative to where arcagent.toml lives.
            team_root = self._workspace.parent / team_root

        return team_root

    @property
    def name(self) -> str:
        return "messaging"

    async def startup(self, ctx: ModuleContext) -> None:
        """Bootstrap arcteam services, register entity, register tools, start polling."""
        # Lazy imports — arcteam is an optional dependency
        from arcteam.audit import AuditLogger
        from arcteam.messenger import MessagingService
        from arcteam.registry import EntityRegistry
        from arcteam.storage import FileBackend
        from arcteam.types import Entity, EntityType

        from arcagent.modules.messaging.tools import create_messaging_tools

        # Resolve entity identity from config, falling back to agent name.
        entity_id = self._config.entity_id
        if not entity_id:
            agent_name = ctx.config.agent.name
            entity_id = f"agent://{agent_name}"
            self._config = self._config.model_copy(
                update={"entity_id": entity_id},
            )

        entity_name = self._config.entity_name or ctx.config.agent.name

        # Bootstrap arcteam services against shared team root.
        team_root = self._resolve_team_root()

        backend = FileBackend(team_root)
        audit = AuditLogger(
            backend,
            hmac_key=self._config.audit_hmac_key.encode("utf-8"),
        )
        await audit.initialize()

        self._registry = EntityRegistry(backend, audit)
        self._svc = MessagingService(backend, self._registry, audit)

        # Auto-register this agent in the team registry.
        if self._config.auto_register:
            entity = Entity(
                id=entity_id,
                name=entity_name,
                type=EntityType.AGENT,
                roles=self._config.roles,
                capabilities=self._config.capabilities,
            )
            try:
                await self._registry.register(entity)
                _logger.info(
                    "Registered entity %s (roles=%s, caps=%s)",
                    entity_id,
                    self._config.roles,
                    self._config.capabilities,
                )
            except ValueError:
                # Already registered from a previous session — update status.
                await self._registry.update_status(entity_id, "online")
                _logger.info("Entity %s already registered, marked online", entity_id)

        # Register LLM-callable tools.
        tools = create_messaging_tools(
            svc=self._svc,
            registry=self._registry,
            config=self._config,
        )
        for tool in tools:
            ctx.tool_registry.register(tool)

        # Subscribe to prompt assembly for context injection.
        ctx.bus.subscribe(
            "agent:assemble_prompt",
            self._on_assemble_prompt,
            priority=50,
        )

        # Subscribe to lifecycle events.
        ctx.bus.subscribe("agent:shutdown", self._on_agent_shutdown)

        # Start background polling loop.
        self._poll_task = asyncio.create_task(self._poll_loop())

        _logger.info("Messaging module started (entity=%s, team_root=%s)", entity_id, team_root)

    async def shutdown(self) -> None:
        """Stop polling loop. Safe to call multiple times."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        _logger.info("Messaging module stopped")

    async def _on_agent_shutdown(self, event: Any) -> None:
        """Handle agent:shutdown event."""
        await self.shutdown()

    async def _on_assemble_prompt(self, ctx: EventContext) -> None:
        """Inject team messaging behavior into the system prompt."""
        sections = ctx.data.get("sections", {})

        entity_id = self._config.entity_id
        entity_name = self._config.entity_name or entity_id

        lines = [
            "## Team Messaging",
            "",
            f"You are **{entity_name}** (`{entity_id}`) on a team.",
            "",
            "### Autonomy Principle",
            "",
            "You are an autonomous agent. Work silently and efficiently.",
            "**Do NOT narrate your actions or report routine status.**",
            "Only contact the user (`notify_user`) when you have:",
            "- A meaningful result or finding worth sharing",
            "- A question that requires human judgment",
            "- A blocker that needs human intervention",
            "",
            "If your inbox is empty or a routine check has no findings, "
            "just move on. No notification needed.",
        ]

        # Unread messages — check them, but don't make it a big deal.
        if self._last_unread:
            total = sum(self._last_unread.values())
            lines.append("")
            lines.append(f"You have {total} unread message(s). "
                         "Check inbox and handle them.")
            for stream, count in self._last_unread.items():
                lines.append(f"  - {stream}: {count}")

        # Behavioral rules — concise.
        lines.extend([
            "",
            "### Communication Rules",
            "",
            "- Reply to `action_required: true` DMs promptly.",
            "- Channel messages are FYI — only respond if relevant to your role.",
            "- Use `thread_id` from the original message when replying in threads.",
            "- If stuck, message the relevant teammate. Don't work in silence.",
            "- Use `notify_user` for the human. Use `messaging_send` for agents/channels.",
        ])

        sections["messaging"] = "\n".join(lines)

    async def _poll_loop(self) -> None:
        """Background polling loop. Updates unread cache for prompt injection."""
        interval = self._config.poll_interval_seconds
        entity_id = self._config.entity_id

        # Wait for services to be ready before first poll.
        await asyncio.sleep(1.0)

        while True:
            try:
                inbox = await self._svc.poll_all(
                    entity_id,
                    max_per_stream=self._config.max_messages_per_poll,
                )
                # Update cached unread counts.
                self._last_unread = {
                    stream: len(msgs) for stream, msgs in inbox.items()
                }
            except Exception:
                _logger.exception("Messaging poll error")
            await asyncio.sleep(interval)


__all__ = ["MessagingModule"]
