"""Markdown Memory Module — 3-tier persistent memory via Module Bus.

Single Module Bus subscriber that routes events to internal helper classes:
- NoteManager: append-only daily notes
- ContextGuard: context.md token budget enforcement
- IdentityAuditor: identity.md audit trail

Hook routing uses convention-based workspace-relative path matching.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from collections.abc import Coroutine
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from arcagent.core.config import EvalConfig, MemoryConfig
from arcagent.core.module_bus import EventContext, ModuleContext
from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.modules.memory.entity_extractor import EntityExtractor
from arcagent.modules.memory.hybrid_search import HybridSearch
from arcagent.modules.memory.policy_engine import PolicyEngine
from arcagent.utils import load_eval_model
from arcagent.utils.io import CHARS_PER_TOKEN

_logger = logging.getLogger("arcagent.modules.memory")

# Memory-protected workspace subpaths (shared constant)
_MEMORY_SUBPATHS = (
    "notes/",
    "identity.md",
    "policy.md",
    "context.md",
    "entities/",
)

# Maximum background tasks before dropping new ones (backpressure)
_MAX_BACKGROUND_QUEUE = 10

# Maximum identity audit snapshots retained (prevents unbounded growth)
_MAX_AUDIT_SNAPSHOTS = 50

# Default timeout for background eval tasks
_BACKGROUND_TASK_TIMEOUT = 120.0


class NoteManager:
    """Manages daily notes with append-only enforcement.

    Notes are stored as workspace/notes/YYYY-MM-DD.md. Only the edit
    tool is allowed (appending). Write and bash operations are vetoed.
    """

    def __init__(self, workspace: Path, config: MemoryConfig) -> None:
        self._notes_dir = workspace / "notes"
        self._config = config

    def enforce_append_only(self, ctx: EventContext, tool_name: str) -> None:
        """Veto non-append operations on notes files."""
        if tool_name == "write":
            ctx.veto("Notes are append-only. Use 'edit' to append content.")
        elif tool_name == "bash":
            ctx.veto("Notes are append-only. Cannot modify via bash.")
        # 'edit' and 'read' are allowed

    async def get_recent_notes(self) -> str:
        """Read today + yesterday notes, apply token budgets."""
        today = date.today()
        yesterday = today - timedelta(days=1)

        parts: list[str] = []

        today_file = self._notes_dir / f"{today.isoformat()}.md"
        if today_file.exists():
            content = today_file.read_text(encoding="utf-8")
            content = self._truncate_to_tokens(content, self._config.notes_budget_today_tokens)
            parts.append(f"### Today ({today.isoformat()})\n\n{content}")

        yesterday_file = self._notes_dir / f"{yesterday.isoformat()}.md"
        if yesterday_file.exists():
            content = yesterday_file.read_text(encoding="utf-8")
            content = self._truncate_to_tokens(content, self._config.notes_budget_yesterday_tokens)
            parts.append(f"### Yesterday ({yesterday.isoformat()})\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _truncate_to_tokens(text: str, max_tokens: int) -> str:
        """Truncate text to approximate token budget."""
        max_chars = max_tokens * CHARS_PER_TOKEN
        if len(text) <= max_chars:
            return text
        return text[:max_chars]


class ContextGuard:
    """Enforces token budget on context.md writes.

    If content exceeds the budget, auto-truncates from the top
    (oldest entries), keeping the most recent content.
    """

    def __init__(self, budget_tokens: int) -> None:
        self._budget = budget_tokens

    async def enforce_budget(self, ctx: EventContext, args: dict[str, Any]) -> None:
        """Check if write would exceed context.md budget.

        If over budget: auto-truncate oldest entries, keep within budget.
        """
        content = args.get("content", "")
        if not content:
            return

        estimated = len(content) // CHARS_PER_TOKEN
        if estimated <= self._budget:
            return

        # Truncate from the top (oldest entries), keep recent
        lines = content.split("\n")
        kept: list[str] = []
        token_count = 0
        for line in reversed(lines):
            line_tokens = max(len(line) // CHARS_PER_TOKEN, 1) if line else 0
            if token_count + line_tokens > self._budget:
                break
            kept.append(line)
            token_count += line_tokens

        kept.reverse()
        args["content"] = "\n".join(kept)


class IdentityAuditor:
    """Captures before/after state for identity.md changes.

    Provides dual audit: telemetry events + JSONL file (NIST AU-9).
    Uses per-trace snapshots to handle concurrent writes safely.
    """

    def __init__(self, workspace: Path, telemetry: Any) -> None:
        self._workspace = workspace
        self._telemetry = telemetry
        self._audit_dir = workspace / "audit"
        self._before_snapshots: dict[str, str] = {}

    async def capture_before(self, ctx: EventContext, path: Path) -> None:
        """Snapshot current identity.md content before write.

        Caps snapshot count to prevent unbounded growth from orphaned traces.
        """
        # Evict oldest snapshots if over limit
        while len(self._before_snapshots) >= _MAX_AUDIT_SNAPSHOTS:
            oldest_key = next(iter(self._before_snapshots))
            del self._before_snapshots[oldest_key]

        if path.exists():
            self._before_snapshots[ctx.trace_id] = path.read_text(encoding="utf-8")
        else:
            self._before_snapshots[ctx.trace_id] = ""

    async def capture_after(self, ctx: EventContext, path: Path) -> None:
        """Log the change after write succeeds."""
        before = self._before_snapshots.pop(ctx.trace_id, "")
        after_content = path.read_text(encoding="utf-8") if path.exists() else ""
        if after_content == before:
            return  # No actual change

        # Telemetry audit event
        self._telemetry.audit_event(
            "identity.modified",
            {
                "before_length": len(before),
                "after_length": len(after_content),
                "session_id": ctx.data.get("session_id", ""),
            },
        )

        # JSONL defense-in-depth (NIST AU-9)
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        audit_file = self._audit_dir / "identity-changes.jsonl"
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "agent_did": ctx.agent_did,
            "before": before,
            "after": after_content,
            "session_id": ctx.data.get("session_id", ""),
        }
        with open(audit_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


class MarkdownMemoryModule:
    """Module Bus subscriber providing 3-tier persistent memory.

    Implements the Module protocol. Delegates to:
    - NoteManager: append-only daily notes
    - ContextGuard: context.md token budget enforcement
    - IdentityAuditor: identity.md audit trail
    - EntityExtractor: async LLM-driven entity extraction
    - PolicyEngine: ACE-based self-learning policy
    """

    def __init__(
        self,
        config: MemoryConfig,
        eval_config: EvalConfig,
        telemetry: Any,
        workspace: Path,
        eval_model: Any | None = None,
    ) -> None:
        self._config = config
        self._eval_config = eval_config
        self._telemetry = telemetry
        self._workspace = workspace.resolve()
        self._eval_model = eval_model

        # Internal helpers
        self._notes = NoteManager(workspace, config)
        self._context_guard = ContextGuard(config.context_budget_tokens)
        self._identity_auditor = IdentityAuditor(workspace, telemetry)
        self._entity_extractor = EntityExtractor(
            eval_config=eval_config,
            workspace=workspace,
            telemetry=telemetry,
        )
        self._policy_engine = PolicyEngine(
            eval_config=eval_config,
            workspace=workspace,
            telemetry=telemetry,
            memory_config=config,
        )

        self._background_tasks: set[asyncio.Task[None]] = set()
        self._hook_active: bool = False
        self._session_messages: list[dict[str, Any]] = []
        self._turn_count: int = 0
        self._llm_config: Any = None
        self._semaphore = asyncio.Semaphore(eval_config.max_concurrent)
        self._hybrid_search = HybridSearch(workspace, config)

    @property
    def name(self) -> str:
        return "memory"

    async def startup(self, ctx: ModuleContext) -> None:
        """Register all event handlers with the module bus."""
        bus = ctx.bus
        self._llm_config = ctx.llm_config
        self._register_search_tool(ctx.tool_registry)
        bus.subscribe("agent:pre_tool", self._on_pre_tool, priority=10)
        bus.subscribe("agent:post_tool", self._on_post_tool, priority=100)
        bus.subscribe(
            "agent:assemble_prompt",
            self._on_assemble_prompt,
            priority=50,
        )
        bus.subscribe("agent:post_respond", self._on_post_respond, priority=100)
        bus.subscribe("agent:shutdown", self._on_shutdown, priority=50)

    def _get_eval_model(self) -> Any:
        """Lazy-init eval model from config, fallback to agent's LLM config.

        Respects ``EvalConfig.fallback_behavior``:
        - ``"skip"``: return None on failure (default)
        - ``"error"``: raise on failure
        """
        if self._eval_model is not None:
            return self._eval_model

        eval_cfg = self._eval_config
        if eval_cfg.provider and eval_cfg.model:
            model_id = f"{eval_cfg.provider}/{eval_cfg.model}"
        elif self._llm_config is not None:
            model_id = self._llm_config.model
        else:
            if eval_cfg.fallback_behavior == "error":
                msg = "No eval model config and no LLM config fallback"
                raise RuntimeError(msg)
            _logger.warning("No eval model config and no LLM config fallback")
            return None

        try:
            self._eval_model = load_eval_model(model_id)
        except Exception:
            if eval_cfg.fallback_behavior == "error":
                raise
            _logger.exception("Failed to load eval model: %s", model_id)
            return None
        return self._eval_model

    def _register_search_tool(self, tool_registry: Any) -> None:
        """Register memory_search as a callable tool."""
        tool = RegisteredTool(
            name="memory_search",
            description="Search agent memory across notes, entities, and context.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                        "maxLength": 500,
                    },
                    "scope": {
                        "type": "string",
                        "description": "Filter: notes, entities, context",
                        "enum": ["notes", "entities", "context"],
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Start date (YYYY-MM-DD). Reserved.",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "End date (YYYY-MM-DD). Reserved.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=self._handle_memory_search,
        )
        tool_registry.register(tool)

    async def _handle_memory_search(
        self,
        query: str,
        scope: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> str:
        """Handle memory_search tool invocation.

        Results are wrapped in boundary markers to prevent prompt
        injection from stored memory content (LLM-01 mitigation).
        """
        results = await self._hybrid_search.search(
            query=query,
            scope=scope,
            date_from=date_from,
            date_to=date_to,
        )
        if not results:
            return "No memory results found."
        parts = []
        for r in results:
            # Boundary markers prevent result content from being
            # interpreted as instructions (prompt injection defense)
            parts.append(
                f"<memory-result source=\"{r.source}\" score=\"{r.score:.2f}\">\n"
                f"{r.content}\n"
                f"</memory-result>"
            )
        return "\n".join(parts)

    async def shutdown(self) -> None:
        """Cancel background tasks, close resources."""
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        await self._hybrid_search.close()

    async def _on_pre_tool(self, ctx: EventContext) -> None:
        """Route pre-tool events based on target file path."""
        if self._hook_active:
            return  # Guard re-entrancy

        tool_name = ctx.data.get("tool", "")
        args = ctx.data.get("args", {})

        # SEC-001: Deny-by-default for bash commands targeting memory.
        # All memory files must be modified via read/write/edit, not bash.
        if tool_name == "bash":
            cmd = args.get("command", "")
            if self._bash_targets_memory(cmd):
                ctx.veto("Memory files must be modified via read/write/edit tools, not bash.")
            return

        path = self._resolve_path(tool_name, args)
        if path is None:
            return

        self._hook_active = True
        try:
            rel_str = str(path.relative_to(self._workspace)).replace("\\", "/")

            if rel_str.startswith("notes/"):
                self._notes.enforce_append_only(ctx, tool_name)
            elif rel_str == "context.md":
                await self._context_guard.enforce_budget(ctx, args)
            elif rel_str == "identity.md":
                await self._identity_auditor.capture_before(ctx, path)
        except ValueError:
            pass  # Path not under workspace — ignore
        finally:
            self._hook_active = False

    async def _on_post_tool(self, ctx: EventContext) -> None:
        """Handle post-tool events for identity audit logging."""
        tool_name = ctx.data.get("tool", "")
        args = ctx.data.get("args", {})
        path = self._resolve_path(tool_name, args)
        if path is None:
            return

        try:
            rel = str(path.relative_to(self._workspace))
            if rel == "identity.md":
                await self._identity_auditor.capture_after(ctx, path)
        except ValueError:
            pass

    async def _on_assemble_prompt(self, ctx: EventContext) -> None:
        """Inject recent notes and memory guidance into system prompt."""
        sections = ctx.data.get("sections", {})

        # Inject notes
        notes_content = await self._notes.get_recent_notes()
        if notes_content:
            sections["notes"] = notes_content

        # Inject memory guidance if identity.md doesn't override
        identity_content = sections.get("identity", "")
        if "## Memory" not in identity_content:
            sections["memory_guidance"] = self._default_memory_guidance()

    @staticmethod
    def _default_memory_guidance() -> str:
        """Default memory guidance text for agents.

        Intentionally minimal — avoids exposing internal architecture
        details that could be leveraged for prompt injection (LLM-07).
        """
        return (
            "## Memory\n\n"
            "You have persistent memory across sessions.\n\n"
            "- Use `memory_search` to recall past conversations and facts.\n"
            "- Use `edit` to append observations to today's notes.\n"
            "- Use `write` or `edit` to update your working context."
        )

    async def _on_post_respond(self, ctx: EventContext) -> None:
        """Fire async entity extraction and periodic policy evaluation."""
        model = self._get_eval_model()
        if model is None:
            return

        messages = ctx.data.get("messages", [])
        if not messages:
            return

        session_id = ctx.data.get("session_id", "")

        # Accumulate messages for session-end policy evaluation
        self._session_messages = messages

        # Entity extraction on every response
        if self._config.entity_extraction_enabled:
            self._spawn_background(self._entity_extractor.extract(messages, model))

        # Policy evaluation at configured interval
        self._turn_count += 1
        interval = self._config.policy_eval_interval_turns
        if self._turn_count % interval == 0:
            self._spawn_background(
                self._policy_engine.evaluate(messages, model, session_id=session_id)
            )

    async def _on_shutdown(self, ctx: EventContext) -> None:
        """Run policy evaluation once at session end."""
        if not self._session_messages:
            return

        model = self._get_eval_model()
        if model is None:
            return

        session_id = ctx.data.get("session_id", "")
        await self._policy_engine.evaluate(
            self._session_messages, model, session_id=session_id
        )

    def _spawn_background(self, coro: Coroutine[Any, Any, None]) -> None:
        """Fire-and-forget with semaphore, timeout, backpressure, and logging."""
        # Backpressure: drop tasks when queue is full
        if len(self._background_tasks) >= _MAX_BACKGROUND_QUEUE:
            _logger.warning(
                "Background task queue full (%d), dropping task",
                _MAX_BACKGROUND_QUEUE,
            )
            coro.close()  # Prevent ResourceWarning
            return

        async def _semaphore_wrapped() -> None:
            async with self._semaphore:
                await asyncio.wait_for(coro, timeout=_BACKGROUND_TASK_TIMEOUT)

        task = asyncio.create_task(_semaphore_wrapped())
        self._background_tasks.add(task)

        def _on_done(t: asyncio.Task[None]) -> None:
            self._background_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                self._telemetry.audit_event(
                    "memory.background_error",
                    {
                        "error": str(exc),
                        "type": type(exc).__name__,
                    },
                )

        task.add_done_callback(_on_done)

    def _resolve_path(self, tool_name: str, args: dict[str, Any]) -> Path | None:
        """Extract and canonicalize file path from tool args."""
        if tool_name in ("read", "write", "edit"):
            raw = args.get("path") or args.get("file_path")
            if raw:
                return Path(raw).resolve()
        elif tool_name == "bash":
            cmd = args.get("command", "")
            return self._parse_bash_target(cmd)
        return None

    def _parse_bash_target(self, command: str) -> Path | None:
        """Parse bash command for file operations targeting memory paths.

        Detects: echo > path, cat > path, rm path, mv path, cp path.
        Returns resolved path if it targets a memory path.
        """
        try:
            tokens = shlex.split(command)
        except ValueError:
            return None

        for i, token in enumerate(tokens):
            if token in (">", ">>") and i + 1 < len(tokens):
                target = Path(tokens[i + 1]).resolve()
                if self._is_memory_path(target):
                    return target
            elif token in ("rm", "mv", "cp") and i + 1 < len(tokens):
                target = Path(tokens[i + 1]).resolve()
                if self._is_memory_path(target):
                    return target
        return None

    def _bash_targets_memory(self, command: str) -> bool:
        """Deny-by-default: detect bash commands referencing memory paths.

        Checks workspace-absolute paths and resolves path-like tokens.
        Also detects dangerous commands (sed, awk, perl, tee) that could
        modify files via piping or in-place editing.
        On parse failure, falls back to substring matching for safety.
        """
        ws_str = str(self._workspace)

        # Fast path: absolute workspace path + memory subpath
        for sub in _MEMORY_SUBPATHS:
            if f"{ws_str}/{sub}" in command:
                return True

        # Token-level: resolve each path-like token
        try:
            tokens = shlex.split(command)
        except ValueError:
            # Malformed shell — check substrings for safety
            return any(sub in command for sub in _MEMORY_SUBPATHS)

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

        return False

    def _is_memory_path(self, path: Path) -> bool:
        """Check if path is under workspace memory directories."""
        try:
            rel = str(path.relative_to(self._workspace)).replace("\\", "/")
            return any(
                rel.startswith(sub) if sub.endswith("/") else rel == sub
                for sub in _MEMORY_SUBPATHS
            )
        except ValueError:
            return False
