"""Decorator-form memory module — SPEC-021 task 3.1.

Six ``@hook`` functions mirror :class:`MarkdownMemoryModule`'s
``startup`` registrations plus the Module-protocol shutdown:

  * ``agent:assemble_prompt`` (priority 50) — inject notes + guidance.
  * ``agent:pre_tool``        (priority 10) — bash deny + path routing.
  * ``agent:post_tool``       (priority 100) — identity audit capture.
  * ``agent:post_respond``    (priority 100) — stage messages for
    background entity extraction; ensure today's notes file exists.
  * ``agent:pre_compaction``  (priority 50) — pre-compaction notes flush.
  * ``agent:shutdown``        (priority 100) — drain background tasks,
    close hybrid-search resources.

One ``@tool`` is exposed: ``memory_search`` over notes/entities/context.

One ``@background_task`` (``entity_extraction_loop``) periodically
drains the latest user/assistant pair captured by ``post_respond`` and
runs the eval-model-driven entity extractor. Modeling extraction as a
periodic background task (rather than a per-event spawn) lets the
loader manage its lifecycle via the standard drain-then-replace path
(R-062) and bounds concurrency through the runtime semaphore.

State is shared via :mod:`arcagent.modules.memory._runtime`; the agent
configures it once at startup and the capabilities read it lazily.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from datetime import date
from pathlib import Path
from typing import Any

from arcagent.modules.memory import _runtime
from arcagent.modules.memory.markdown_memory import _MEMORY_SUBPATHS
from arcagent.tools._decorator import background_task, hook, tool
from arcagent.utils.model_helpers import get_eval_model, spawn_background

_logger = logging.getLogger("arcagent.modules.memory.capabilities")

# Tool names that interact with paths via the standard "path"/"file_path"
# kwargs. Bash is handled separately because its target is embedded in
# the command string.
_PATH_TOOLS = ("read", "write", "edit")

# Bash commands that can mutate files via redirection, in-place editing,
# or piping — denied if their target resolves under a memory subpath
# even when the path is referenced indirectly.
_DANGEROUS_BASH_CMDS = frozenset({"sed", "awk", "perl", "tee", "dd", "truncate"})

# Background loop tick rate. Short enough to feel responsive after a
# turn ends, long enough to be cheap when idle.
_ENTITY_EXTRACTION_INTERVAL = 1.0


def _eval_model() -> Any:
    """Lazy-init eval model with cache via ``_runtime.state``."""
    st = _runtime.state()
    result = get_eval_model(
        cached_model=st.eval_model,
        eval_config=st.eval_config,
        llm_config=st.llm_config,
        logger=_logger,
        agent_label=st.eval_label,
    )
    if result is not None:
        st.eval_model = result
    return result


def _is_memory_path(path: Path, workspace: Path) -> bool:
    """Check if ``path`` is under a memory-protected subpath of workspace."""
    try:
        rel = str(path.relative_to(workspace)).replace("\\", "/")
    except ValueError:
        return False
    return any(
        rel.startswith(sub) if sub.endswith("/") else rel == sub for sub in _MEMORY_SUBPATHS
    )


def _resolve_target(tool_name: str, args: dict[str, Any]) -> Path | None:
    """Extract the file path a tool call targets, if any."""
    if tool_name in _PATH_TOOLS:
        raw = args.get("path") or args.get("file_path")
        return Path(raw).resolve() if raw else None
    if tool_name == "bash":
        return _parse_bash_target(args.get("command", ""))
    return None


def _parse_bash_target(command: str) -> Path | None:
    """Parse a bash command for an explicit memory-path target.

    Detects ``echo > path``, ``cat > path``, ``rm path``, ``mv path``,
    ``cp path``. Returns the resolved path only if it falls inside a
    memory subpath of the workspace.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    workspace = _runtime.state().workspace
    for i, token in enumerate(tokens):
        if token in (">", ">>") and i + 1 < len(tokens):
            target = Path(tokens[i + 1]).resolve()
            if _is_memory_path(target, workspace):
                return target
        elif token in ("rm", "mv", "cp") and i + 1 < len(tokens):
            target = Path(tokens[i + 1]).resolve()
            if _is_memory_path(target, workspace):
                return target
    return None


def _bash_targets_memory(command: str) -> bool:
    """Deny-by-default detector for bash commands referencing memory paths.

    Mirrors :meth:`MarkdownMemoryModule._bash_targets_memory`: checks
    workspace-absolute paths first, then resolves path-like tokens, then
    falls back on substring matching for malformed input. Dangerous
    file-mutating commands (sed/awk/perl/tee/dd/truncate) trigger denial
    if any memory subpath name appears anywhere in the command.
    """
    workspace = _runtime.state().workspace
    ws_str = str(workspace)
    for sub in _MEMORY_SUBPATHS:
        if f"{ws_str}/{sub}" in command:
            return True
    try:
        tokens = shlex.split(command)
    except ValueError:
        return any(sub in command for sub in _MEMORY_SUBPATHS)
    has_dangerous_cmd = any(t in _DANGEROUS_BASH_CMDS for t in tokens)
    for token in tokens:
        if "/" not in token and not token.endswith((".md", ".jsonl")):
            continue
        try:
            resolved = Path(token).resolve()
            if _is_memory_path(resolved, workspace):
                return True
        except (ValueError, OSError):
            continue
    if has_dangerous_cmd:
        return any(sub.rstrip("/") in command for sub in _MEMORY_SUBPATHS)
    return False


def _ensure_daily_notes(extra_content: str = "") -> None:
    """Create today's notes file if it doesn't exist."""
    workspace = _runtime.state().workspace
    today = date.today()
    notes_file = workspace / "notes" / f"{today.isoformat()}.md"
    if notes_file.exists():
        return
    notes_file.parent.mkdir(parents=True, exist_ok=True)
    content = f"# Daily Notes - {today.isoformat()}\n\n"
    if extra_content:
        content += extra_content
    notes_file.write_text(content, encoding="utf-8")
    _logger.debug("Auto-created daily notes file: %s", notes_file.name)


def _default_memory_guidance() -> str:
    """Default memory guidance text (LLM-07: avoid leaking internals)."""
    return (
        "## Memory\n\n"
        "You have persistent memory across sessions.\n\n"
        "- Use `memory_search` to recall past conversations and facts.\n"
        "- Use `edit` to append observations to today's notes.\n"
        "- Use `write` or `edit` to update your working context."
    )


# --- Hooks ---------------------------------------------------------------


@hook(event="agent:assemble_prompt", priority=50)
async def inject_memory_sections(ctx: Any) -> None:
    """Inject recent notes and memory guidance into prompt sections."""
    sections = ctx.data.get("sections")
    if sections is None or not isinstance(sections, dict):
        return
    st = _runtime.state()
    notes_content = await st.notes.get_recent_notes()
    if notes_content:
        sections["notes"] = notes_content
    identity_content = sections.get("identity", "")
    if "## Memory" not in identity_content:
        sections["memory_guidance"] = _default_memory_guidance()


@hook(event="agent:pre_tool", priority=10)
async def memory_pre_tool(ctx: Any) -> None:
    """Veto bad memory writes; enforce note/context/identity policies."""
    st = _runtime.state()
    if st.hook_active:
        return  # Guard re-entrancy
    tool_name = ctx.data.get("tool", "")
    args = ctx.data.get("args", {})

    # SEC-001: deny-by-default for bash commands targeting memory.
    if tool_name == "bash":
        cmd = args.get("command", "")
        if _bash_targets_memory(cmd):
            ctx.veto("Memory files must be modified via read/write/edit tools, not bash.")
        return

    path = _resolve_target(tool_name, args)
    if path is None:
        return

    st.hook_active = True
    try:
        rel_str = str(path.relative_to(st.workspace)).replace("\\", "/")
        if rel_str.startswith("notes/"):
            st.notes.enforce_append_only(ctx, tool_name)
        elif rel_str == "context.md":
            await st.context_guard.enforce_budget(ctx, args)
        elif rel_str == "identity.md":
            await st.identity_auditor.capture_before(ctx, path)
    except ValueError:
        pass  # Path not under workspace — ignore
    finally:
        st.hook_active = False


@hook(event="agent:post_tool", priority=100)
async def memory_post_tool(ctx: Any) -> None:
    """Capture after-state for identity.md writes (audit trail)."""
    st = _runtime.state()
    tool_name = ctx.data.get("tool", "")
    args = ctx.data.get("args", {})
    path = _resolve_target(tool_name, args)
    if path is None:
        return
    try:
        rel = str(path.relative_to(st.workspace))
    except ValueError:
        return
    if rel == "identity.md":
        await st.identity_auditor.capture_after(ctx, path)


@hook(event="agent:post_respond", priority=100)
async def memory_post_respond(ctx: Any) -> None:
    """Stage messages for background extraction; ensure daily notes file."""
    st = _runtime.state()
    messages = ctx.data.get("messages", [])
    if not messages:
        return

    _ensure_daily_notes()

    if st.config.entity_extraction_enabled:
        # Latest pair wins — the background loop drains it.
        st.pending_messages = list(messages)


@hook(event="agent:pre_compaction", priority=50)
async def memory_pre_compaction(ctx: Any) -> None:
    """Pre-compaction memory flush: nudge agent to record context."""
    ratio = ctx.data.get("ratio", 0.0)
    _logger.info(
        "Pre-compaction triggered at %.1f%% context usage - ensuring daily notes exist",
        ratio * 100,
    )
    _ensure_daily_notes(
        "**Context approaching limit** - important information should be noted here.\n\n",
    )


@hook(event="agent:shutdown", priority=100)
async def memory_shutdown(ctx: Any) -> None:
    """Await in-flight background tasks; close hybrid-search resources."""
    del ctx  # event payload unused
    st = _runtime.state()
    if st.background_tasks:
        _logger.info(
            "Awaiting %d memory background task(s) before shutdown",
            len(st.background_tasks),
        )
        await asyncio.gather(*st.background_tasks, return_exceptions=True)
    await st.hybrid_search.close()


# --- Tool ---------------------------------------------------------------


@tool(
    name="memory_search",
    description="Search agent memory across notes, entities, and context.",
    classification="read_only",
    when_to_use="Recall facts, past conversations, or stored context.",
)
async def memory_search(
    query: str,
    scope: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> str:
    """Search memory and return XML-wrapped results.

    Results are wrapped in ``<memory-result>`` boundary markers to
    prevent stored content from being interpreted as instructions
    (LLM-01 prompt-injection mitigation).
    """
    st = _runtime.state()
    results = await st.hybrid_search.search(
        query=query,
        scope=scope,
        date_from=date_from,
        date_to=date_to,
    )
    if not results:
        return "No memory results found."
    parts = [
        f'<memory-result source="{r.source}" score="{r.score:.2f}">\n{r.content}\n</memory-result>'
        for r in results
    ]
    return "\n".join(parts)


# --- Background task ----------------------------------------------------


@background_task(
    name="entity_extraction_loop",
    interval=_ENTITY_EXTRACTION_INTERVAL,
)
async def entity_extraction_loop(_ctx: Any) -> None:
    """Drain pending messages and run entity extraction periodically.

    Sleeps ``_ENTITY_EXTRACTION_INTERVAL`` seconds between checks; if
    ``post_respond`` has staged a fresh message list, dispatch the
    extractor through the shared semaphore. Eval-model load failures
    short-circuit the iteration without raising — extraction is
    best-effort and must never crash the agent loop.
    """
    while True:
        try:
            await _drain_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.warning("entity extraction loop iteration failed", exc_info=True)
        await asyncio.sleep(_ENTITY_EXTRACTION_INTERVAL)


async def _drain_once() -> None:
    """Run one iteration of the entity-extraction drain."""
    st = _runtime.state()
    if not st.config.entity_extraction_enabled:
        return
    if not st.pending_messages:
        return
    model = _eval_model()
    if model is None:
        # No model available — drop the staged batch so we don't loop
        # on the same content forever.
        st.pending_messages = []
        return
    messages = st.pending_messages
    st.pending_messages = []
    # configure() always installs a semaphore; this guard pins the
    # type for mypy and fails loudly if anyone bypasses configure().
    semaphore = st.semaphore
    if semaphore is None:
        raise RuntimeError("memory module semaphore not configured")
    spawn_background(
        st.entity_extractor.extract(messages, model),
        background_tasks=st.background_tasks,
        semaphore=semaphore,
        eval_config=st.eval_config,
        telemetry=st.telemetry,
        audit_event_name="memory.background_error",
        logger=_logger,
    )


__all__ = [
    "entity_extraction_loop",
    "inject_memory_sections",
    "memory_post_respond",
    "memory_post_tool",
    "memory_pre_compaction",
    "memory_pre_tool",
    "memory_search",
    "memory_shutdown",
]
