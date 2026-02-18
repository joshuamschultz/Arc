"""Messaging module — inter-agent communication via ArcTeam.

Integrates the arcteam messaging subsystem as an arcagent module.
Provides tools for the LLM, background inbox polling, and context
injection of unread message summaries into the system prompt.

Zero hardcoded coupling to arcagent core — this is a pure module.
"""

from __future__ import annotations

import asyncio
import json
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

        from arcagent.modules.messaging.tools import (
            create_messaging_tools,
            create_task_tools,
        )

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
        task_tools = create_task_tools(
            svc=self._svc,
            config=self._config,
            workspace=self._workspace,
        )
        for tool in tools + task_tools:
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

    def _load_pending_tasks(self) -> list[dict[str, Any]]:
        """Read pending tasks from workspace. Returns incomplete tasks."""
        tasks_path = self._workspace / "tasks.json"
        if not tasks_path.exists():
            return []
        try:
            data = json.loads(tasks_path.read_text(encoding="utf-8"))
            return [t for t in data if t.get("status") != "done"]
        except (json.JSONDecodeError, OSError):
            return []

    async def _on_assemble_prompt(self, ctx: EventContext) -> None:
        """Inject team messaging behavior into the system prompt.

        Includes:
        - Unread message alerts
        - Pending multi-step tasks from workspace
        - Behavioral rules for team communication
        """
        sections = ctx.data.get("sections", {})

        entity_id = self._config.entity_id
        entity_name = self._config.entity_name or entity_id

        lines = [
            "## Team Messaging",
            "",
            f"You are **{entity_name}** (`{entity_id}`) on a team.",
        ]

        # Urgent: unread messages — directive to check first.
        if self._last_unread:
            total = sum(self._last_unread.values())
            lines.append("")
            lines.append(f"**You have {total} unread message(s). "
                         "Call `messaging_check_inbox` BEFORE doing anything else.**")
            for stream, count in self._last_unread.items():
                lines.append(f"  - {stream}: {count}")
            lines.append("")
            lines.append(
                "Read them, then respond to any that are `action_required: true` "
                "or that ask you a question."
            )

        # Surface pending tasks so multi-step work survives across runs.
        pending = self._load_pending_tasks()
        if pending:
            lines.append("")
            lines.append(f"### Pending Tasks ({len(pending)} incomplete)")
            lines.append("")
            for task in pending:
                status = task.get("status", "pending")
                lines.append(
                    f"- **[{status}]** `{task.get('id', '?')}`: "
                    f"{task.get('description', '(no description)')}"
                )
                if task.get("report_to"):
                    lines.append(f"  Report results to: `{task['report_to']}`")
            lines.append("")
            lines.append(
                "Work through pending tasks. Use `task_update` to change "
                "status to `in_progress` when starting, and `task_complete` "
                "when done (auto-sends result to `report_to` if set)."
            )

        # Always: behavioral rules.
        lines.extend([
            "",
            "### Team Communication Rules",
            "",
            "1. **Check inbox first** — At the start of every turn, "
            "call `messaging_check_inbox` to see if teammates need you.",
            "2. **Plan multi-step tasks** — When a message requires multiple "
            "steps (ask X then tell Y), use `task_create` for each step. "
            "Set `report_to` so results auto-send when you call "
            "`task_complete`. Tasks persist across turns.",
            "3. **Share results** — When you complete a task, use "
            "`task_complete` with a result summary. If the task has a "
            "`report_to`, the result is sent automatically.",
            "4. **Ask for help** — If you are stuck or need information another "
            "agent has, message them directly. Don't work in silence.",
            "5. **Reply to DMs** — If a direct message is marked "
            "`action_required: true`, respond promptly.",
            "6. **Channel messages are FYI** — Only respond to channel messages "
            "that are relevant to your role or specifically mention you. "
            "Not every group message needs a reply.",
            "7. **Use channels for team-wide updates** — Post status, "
            "blockers, and completed work to shared channels so the whole "
            "team stays informed.",
            "8. **Thread replies** — When responding to a message in a thread, "
            "pass the `thread_id` from the original message to keep "
            "conversations grouped.",
            "",
            "### Tools",
            "",
            "**Messaging:**",
            "- `messaging_check_inbox` — Check for unread messages "
            "(replies include full thread context)",
            "- `messaging_send` — Send a message (DM, channel, or role)",
            "- `messaging_read_thread` — Read full conversation thread",
            "- `messaging_list_entities` — Discover teammates and capabilities",
            "- `messaging_list_channels` — See available channels",
            "",
            "**Task Management:**",
            "- `task_create` — Create a task (set `report_to` for auto-send)",
            "- `task_list` — List tasks (filter by status)",
            "- `task_update` — Update task status or description",
            "- `task_complete` — Mark done + auto-send result to `report_to`",
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
