"""BioMemoryModule — facade implementing the Module protocol.

Delegates to internal helpers: WorkingMemory, IdentityManager,
Retriever, Consolidator, DeepConsolidator. Registers bus handlers
and memory tools. Optionally integrates with arcteam TeamMemoryService.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path
from typing import Any

from arcagent.core.config import EvalConfig
from arcagent.core.errors import ConfigError
from arcagent.core.module_bus import EventContext, ModuleContext
from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.modules.bio_memory.consolidator import Consolidator
from arcagent.modules.bio_memory.identity_manager import IdentityManager
from arcagent.modules.bio_memory.retriever import Retriever
from arcagent.modules.bio_memory.working_memory import WorkingMemory
from arcagent.utils.model_helpers import get_eval_model, spawn_background
from arcagent.utils.sanitizer import sanitize_text

_logger = logging.getLogger("arcagent.modules.bio_memory")

# Memory-protected workspace subpaths
_MEMORY_SUBPATHS = ("memory/", "entities/")


class BioMemoryModule:
    """Biologically-inspired memory module.

    Facade that delegates to internal helpers:
    - WorkingMemory: scratchpad lifecycle
    - IdentityManager: how-i-work.md injection and updates
    - Retriever: grep + wiki-link graph traversal (memory + entities + team)
    - Consolidator: light consolidation on shutdown (episodes + entity updates)
    - DeepConsolidator: deep "sleep cycle" consolidation (entity rewrites, graph, merge)
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        eval_config: EvalConfig | None = None,
        telemetry: Any = None,
        workspace: Path = Path("."),
        llm_config: Any | None = None,
        team_config: dict[str, Any] | None = None,
    ) -> None:
        self._config = BioMemoryConfig(**(config or {}))
        self._eval_config = eval_config or EvalConfig()
        self._llm_config = llm_config
        self._telemetry = telemetry
        self._workspace = workspace.resolve() if workspace != Path(".") else workspace
        self._team_config = team_config
        self._team_service: Any = None

        self._memory_dir = self._workspace / "memory"
        self._eval_model: Any = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(self._eval_config.max_concurrent)
        self._messages: list[dict[str, Any]] = []

        # Internal helpers
        self._working = WorkingMemory(self._memory_dir, self._config)
        self._identity = IdentityManager(
            self._memory_dir, self._config, telemetry,
        )
        self._retriever = Retriever(
            self._memory_dir,
            self._config,
            workspace=self._workspace,
            team_entities_dir=self._get_team_entities_dir(),
        )
        self._consolidator = Consolidator(
            self._memory_dir,
            self._config,
            self._identity,
            self._working,
            telemetry,
            workspace=self._workspace,
            team_service_factory=self._get_team_service,
        )

    @property
    def name(self) -> str:
        return "bio_memory"

    async def startup(self, ctx: ModuleContext) -> None:
        """Register bus handlers + memory tools."""
        # Mutual exclusivity check
        if ctx.bus.get_module("memory") is not None:
            raise ConfigError(
                code="CONFIG_MODULE_CONFLICT",
                message=(
                    "bio_memory and memory modules are mutually exclusive. "
                    "Disable one in [modules] config."
                ),
            )

        bus = ctx.bus
        bus.subscribe(
            "agent:assemble_prompt", self._on_assemble_prompt, priority=50,
        )
        bus.subscribe(
            "agent:post_respond", self._on_post_respond, priority=100,
        )
        bus.subscribe(
            "agent:pre_tool", self._on_pre_tool, priority=10,
        )
        bus.subscribe(
            "agent:post_tool", self._on_post_tool, priority=100,
        )
        bus.subscribe(
            "agent:shutdown", self._on_shutdown, priority=100,
        )

        self._register_tools(ctx.tool_registry)

    async def shutdown(self) -> None:
        """Await in-flight background tasks (consolidation, etc.) before teardown."""
        if self._background_tasks:
            _logger.info(
                "Awaiting %d background task(s) before shutdown",
                len(self._background_tasks),
            )
            await asyncio.gather(
                *self._background_tasks, return_exceptions=True,
            )

    # -- Team integration --

    def _get_team_service(self) -> Any:
        """Lazy-init TeamMemoryService from arcteam. Returns None if unavailable."""
        if self._team_service is not None:
            return self._team_service
        if self._team_config is None:
            return None
        try:
            from arcteam.memory.config import TeamMemoryConfig  # type: ignore[import-untyped]
            from arcteam.memory.service import TeamMemoryService  # type: ignore[import-untyped]
            team_cfg = TeamMemoryConfig(**self._team_config)
            self._team_service = TeamMemoryService(team_cfg)
            return self._team_service
        except ImportError:
            _logger.debug("arcteam not installed, team memory disabled")
            return None
        except Exception:
            _logger.debug("arcteam init failed, team memory disabled", exc_info=True)
            return None

    def _get_team_entities_dir(self) -> Path | None:
        """Resolve team entities path from team_config if available."""
        if self._team_config is None:
            return None
        root = self._team_config.get("root_path") or self._team_config.get("root")
        if not root:
            return None
        team_entities = Path(root) / "entities"
        if team_entities.exists():
            return team_entities
        return None

    # -- Bus handlers --

    async def _on_assemble_prompt(self, ctx: EventContext) -> None:
        """Inject identity + working memory + entity hint into prompt context."""
        identity_text = await self._identity.inject_context()
        working_text = await self._working.read()

        parts: list[str] = []
        if identity_text:
            parts.append(f"<agent-identity>\n{identity_text}\n</agent-identity>")
        if working_text:
            parts.append(f"<working-memory>\n{working_text}\n</working-memory>")

        # Entity location hint so LLM knows to search
        entities_dir = self._workspace / self._config.entities_dirname
        if entities_dir.exists():
            parts.append(
                "<memory-hint>"
                "Entity files available at workspace/entities/. "
                "Use memory_search with scope='entities' to find relevant entities."
                "</memory-hint>"
            )

        if parts:
            ctx.data.setdefault("memory_context", "\n\n".join(parts))

    async def _on_post_respond(self, ctx: EventContext) -> None:
        """Update working memory after each turn."""
        messages = ctx.data.get("messages", [])
        if messages:
            self._messages = messages

    async def _on_pre_tool(self, ctx: EventContext) -> None:
        """Veto bash commands targeting memory paths."""
        tool_name = ctx.data.get("tool", "")
        if tool_name != "bash":
            return

        cmd = ctx.data.get("args", {}).get("command", "")
        if self._bash_targets_memory(cmd):
            ctx.veto(
                "Memory files must be modified via memory tools, not bash.",
            )

    async def _on_post_tool(self, ctx: EventContext) -> None:
        """Audit event if a write/edit tool modified a memory file."""
        tool_name = ctx.data.get("tool", "")
        if tool_name not in ("write", "edit"):
            return

        file_path = ctx.data.get("args", {}).get("file_path", "")
        if not file_path:
            return

        try:
            resolved = Path(file_path).resolve()
            if self._is_memory_path(resolved):
                self._telemetry.audit_event(
                    "memory.file_modified_by_tool",
                    details={"tool": tool_name, "path": str(resolved)},
                )
        except (ValueError, OSError):
            pass

    async def _on_shutdown(self, ctx: EventContext) -> None:
        """Trigger light consolidation on session end."""
        if not self._config.light_on_shutdown:
            return
        if not self._messages:
            return

        model = self._get_eval_model()
        if model is None:
            _logger.warning("No eval model available, skipping consolidation")
            return

        spawn_background(
            self._consolidator.light_consolidate(self._messages, model),
            background_tasks=self._background_tasks,
            semaphore=self._semaphore,
            eval_config=self._eval_config,
            telemetry=self._telemetry,
            audit_event_name="bio_memory.consolidation_error",
            logger=_logger,
        )

    # -- Tool registration --

    def _register_tools(self, tool_registry: Any) -> None:
        """Register memory tools."""
        tool_registry.register(RegisteredTool(
            name="memory_search",
            description="Search agent memory using grep + wiki-link graph traversal.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (keywords or entity names)",
                        "maxLength": 500,
                    },
                    "scope": {
                        "type": "string",
                        "description": "Filter: episodes, identity, working, entities",
                        "enum": ["episodes", "identity", "working", "entities"],
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum results",
                        "default": 5,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=self._handle_memory_search,
        ))

        tool_registry.register(RegisteredTool(
            name="memory_note",
            description="Record a note — appends to working memory or creates an episode.",
            input_schema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Note content to record",
                        "maxLength": 2000,
                    },
                    "target": {
                        "type": "string",
                        "description": "Where to write: working (default) or episode",
                        "enum": ["working", "episode"],
                        "default": "working",
                    },
                },
                "required": ["content"],
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=self._handle_memory_note,
        ))

        tool_registry.register(RegisteredTool(
            name="memory_recall",
            description="Recall a specific memory by name (episode or entity).",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name or slug of the memory to recall",
                        "maxLength": 200,
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=self._handle_memory_recall,
        ))

        tool_registry.register(RegisteredTool(
            name="memory_reflect",
            description="Trigger a reflection on recent memories to update identity patterns.",
            input_schema={
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "description": "Optional focus area for reflection",
                        "maxLength": 500,
                    },
                },
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=self._handle_memory_reflect,
        ))

        tool_registry.register(RegisteredTool(
            name="memory_consolidate_deep",
            description=(
                "Trigger deep memory consolidation"
                " (entity rewrites, graph analysis, merge detection)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview changes without writing",
                        "default": False,
                    },
                },
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=self._handle_deep_consolidation,
        ))

    # -- Tool handlers --

    async def _handle_memory_search(
        self,
        query: str,
        scope: str | None = None,
        top_k: int = 5,
    ) -> str:
        """Handle memory_search tool invocation."""
        results = await self._retriever.search(
            query=query, top_k=top_k, scope=scope,
        )
        self._telemetry.audit_event(
            "memory.searched",
            details={"query": query[:200], "scope": scope, "results": len(results)},
        )
        if not results:
            return "No memory results found."
        parts = []
        for r in results:
            parts.append(
                f'<memory-result source="{r.source}" score="{r.score:.2f}">\n'
                f"{r.content}\n"
                f"</memory-result>",
            )
        return "\n".join(parts)

    async def _handle_memory_note(
        self,
        content: str,
        target: str = "working",
    ) -> str:
        """Handle memory_note tool invocation."""
        # Sanitize user/agent content before writing (ASI-06)
        clean = sanitize_text(content, max_length=2000)

        if target == "working":
            await self._working.write(
                content=clean,
                frontmatter={"type": "note"},
            )
            self._telemetry.audit_event(
                "memory.note_written",
                details={"target": "working", "length": len(clean)},
            )
            return "Note recorded to working memory."

        if target == "episode":
            # Write a user-initiated episode directly
            from datetime import UTC, datetime

            import yaml

            from arcagent.utils.io import atomic_write_text
            from arcagent.utils.sanitizer import slugify

            timestamp = datetime.now(UTC).strftime("%Y-%m-%d")
            slug = slugify(clean[:50])
            filename = f"{timestamp}-{slug}.md"

            episodes_dir = self._memory_dir / self._config.episodes_dirname
            episodes_dir.mkdir(parents=True, exist_ok=True)
            target_path = episodes_dir / filename

            frontmatter = {"title": slug, "date": timestamp, "tags": ["manual-note"]}
            fm_text = yaml.dump(
                frontmatter, default_flow_style=False, sort_keys=False,
            ).strip()
            episode_content = f"---\n{fm_text}\n---\n\n{clean}\n"
            atomic_write_text(target_path, episode_content)

            self._telemetry.audit_event(
                "memory.note_written",
                details={"target": "episode", "episode_name": filename},
            )
            return f"Episode created: {filename}"

        return f"Unknown target: {target}"

    async def _handle_memory_recall(self, name: str) -> str:
        """Handle memory_recall tool invocation."""
        result = await self._retriever.recall(name)
        self._telemetry.audit_event(
            "memory.recalled",
            details={"name": name[:200], "found": result is not None},
        )
        if result is None:
            return f"No memory found for '{name}'."
        return f'<memory-result source="{name}">\n{result}\n</memory-result>'

    async def _handle_memory_reflect(
        self, focus: str | None = None,
    ) -> str:
        """Handle memory_reflect tool invocation."""
        model = self._get_eval_model()
        if model is None:
            return "Reflection unavailable: no eval model configured."
        # Trigger identity update evaluation using recent messages
        if self._messages:
            current_identity = await self._identity.read()
            new_identity = await self._consolidator.evaluate_identity(
                self._messages, current_identity, model,
            )
            if new_identity is not None:
                await self._identity.update(new_identity)
                self._telemetry.audit_event(
                    "memory.identity_reflected",
                    details={"focus": focus},
                )
                return "Identity updated based on reflection."
        return "No significant changes to identity detected."

    async def _handle_deep_consolidation(
        self, dry_run: bool = False,
    ) -> str:
        """Handle memory_consolidate_deep tool invocation."""
        # Lazy import to avoid circular deps at module level
        from arcagent.modules.bio_memory.deep_consolidator import DeepConsolidator

        model = self._get_eval_model()
        if model is None:
            return "Deep consolidation unavailable: no eval model configured."

        deep = DeepConsolidator(
            memory_dir=self._memory_dir,
            workspace=self._workspace,
            config=self._config,
            identity=self._identity,
            telemetry=self._telemetry,
            team_service_factory=self._get_team_service,
        )

        result = await deep.consolidate(model, agent_id="self")
        self._telemetry.audit_event(
            "memory.deep_consolidated",
            details=result,
        )

        # Format summary for the agent
        if result.get("skipped"):
            return f"Deep consolidation skipped: {result.get('reason', 'unknown')}"

        parts = [f"Deep consolidation complete (intensity: {result.get('intensity', 'unknown')})."]
        ep = result.get("entity_pass", {})
        if ep:
            parts.append(
                f"  Entities rewritten: {ep.get('entities_rewritten', 0)}, "
                f"skipped unchanged: {ep.get('skipped_unchanged', 0)}"
            )
        gp = result.get("graph_pass", {})
        if gp and not gp.get("skipped"):
            parts.append(f"  Graph links added: {gp.get('links_added', 0)}")
        if result.get("merges"):
            parts.append(f"  Merges: {result['merges']}")
        stale = result.get("stale", {})
        if stale:
            parts.append(
                f"  Stale: {stale.get('flagged', 0)} flagged, "
                f"{stale.get('archived', 0)} archived"
            )
        if result.get("identity_refreshed"):
            parts.append("  Identity refreshed.")
        return "\n".join(parts)

    # -- Helpers --

    def _get_eval_model(self) -> Any:
        """Lazy-init eval model, caching result."""
        result = get_eval_model(
            cached_model=self._eval_model,
            eval_config=self._eval_config,
            llm_config=self._llm_config,
            logger=_logger,
        )
        if result is not None:
            self._eval_model = result
        return result

    def _bash_targets_memory(self, cmd: str) -> bool:
        """Deny-by-default: detect bash commands referencing memory paths.

        Uses shlex parsing for accurate token-level path resolution.
        Falls back to substring matching on parse failure for safety.
        """
        ws_str = str(self._workspace)

        # Fast path: absolute workspace path + memory subpath
        for sub in _MEMORY_SUBPATHS:
            if f"{ws_str}/{sub}" in cmd:
                return True

        # Token-level: resolve each path-like token
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            # Malformed shell — check substrings for safety
            return any(sub in cmd for sub in _MEMORY_SUBPATHS)

        # Detect dangerous commands that can modify files via pipes/flags
        dangerous_cmds = {"sed", "awk", "perl", "tee", "dd", "truncate"}
        has_dangerous_cmd = any(t in dangerous_cmds for t in tokens)

        for token in tokens:
            if "/" not in token and not token.endswith((".md", ".jsonl")):
                continue
            try:
                resolved = Path(token).resolve()
                if self._is_memory_path(resolved):
                    return True
            except (ValueError, OSError):
                continue

        # Dangerous commands with memory subpath by name → deny
        if has_dangerous_cmd:
            return any(sub.rstrip("/") in cmd for sub in _MEMORY_SUBPATHS)

        return False

    def _is_memory_path(self, path: Path) -> bool:
        """Check if a resolved path falls within memory or entities directory."""
        mem_dir = self._memory_dir.resolve()
        ent_dir = (self._workspace / self._config.entities_dirname).resolve()
        try:
            path.relative_to(mem_dir)
            return True
        except ValueError:
            pass
        try:
            path.relative_to(ent_dir)
            return True
        except ValueError:
            return False
