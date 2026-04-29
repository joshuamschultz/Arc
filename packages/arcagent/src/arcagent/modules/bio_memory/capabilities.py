"""Decorator-form bio_memory module — SPEC-021.

Five ``@hook`` functions mirror :class:`BioMemoryModule`'s
``startup`` registrations:

  * ``agent:assemble_prompt`` (priority 50)  — inject working memory +
    entity hint into prompt context.
  * ``agent:post_respond``    (priority 100) — track messages; trigger
    periodic consolidation every N turns.
  * ``agent:pre_tool``        (priority 10)  — veto bash commands that
    target memory paths.
  * ``agent:post_tool``       (priority 100) — emit audit event when a
    write/edit tool modifies a memory file.
  * ``agent:shutdown``        (priority 100) — final consolidation
    safety-net; drain background tasks.

Four ``@tool`` functions are exposed:

  * ``memory_search``           — grep + wiki-link graph search.
  * ``memory_note``             — record to working memory or episode.
  * ``memory_recall``           — recall a specific memory by name.
  * ``memory_consolidate_deep`` — trigger deep consolidation.

State is shared via :mod:`arcagent.modules.bio_memory._runtime`; the
agent configures it once at startup and these capabilities read lazily.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path
from typing import Any

from arcagent.modules.bio_memory import _runtime
from arcagent.tools._decorator import hook, tool
from arcagent.utils.model_helpers import get_eval_model, spawn_background

_logger = logging.getLogger("arcagent.modules.bio_memory.capabilities")

# Memory-protected workspace subpaths (mirrors BioMemoryModule._MEMORY_SUBPATHS)
_MEMORY_SUBPATHS = ("memory/", "entities/")


# --- Helpers ---------------------------------------------------------------


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


def _is_memory_path(path: Path, state: _runtime._State) -> bool:
    """Return True if ``path`` falls within the memory or entities directory."""
    mem_dir = state.memory_dir.resolve()
    ent_dir = (state.workspace / state.config.entities_dirname).resolve()
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


def _bash_targets_memory(cmd: str, state: _runtime._State) -> bool:
    """Deny-by-default: detect bash commands referencing memory paths.

    Mirrors :meth:`BioMemoryModule._bash_targets_memory`.
    """
    ws_str = str(state.workspace)

    # Fast path: absolute workspace path + memory subpath
    for sub in _MEMORY_SUBPATHS:
        if f"{ws_str}/{sub}" in cmd:
            return True

    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return any(sub in cmd for sub in _MEMORY_SUBPATHS)

    dangerous_cmds = {"sed", "awk", "perl", "tee", "dd", "truncate"}
    has_dangerous_cmd = any(t in dangerous_cmds for t in tokens)

    for token in tokens:
        if "/" not in token and not token.endswith((".md", ".jsonl")):
            continue
        try:
            resolved = Path(token).resolve()
            if _is_memory_path(resolved, state):
                return True
        except (ValueError, OSError):
            continue

    if has_dangerous_cmd:
        return any(sub.rstrip("/") in cmd for sub in _MEMORY_SUBPATHS)

    return False


# --- Hooks -----------------------------------------------------------------


@hook(event="agent:assemble_prompt", priority=50)
async def bio_memory_assemble_prompt(ctx: Any) -> None:
    """Inject working memory + entity hint into prompt context."""
    st = _runtime.state()
    working_text = await st.working.read()

    parts: list[str] = []
    if working_text:
        parts.append(f"<working-memory>\n{working_text}\n</working-memory>")

    entities_dir = st.workspace / st.config.entities_dirname
    if entities_dir.exists():
        parts.append(
            "<memory-hint>"
            "Entity files available at workspace/entities/. "
            "Use memory_search with scope='entities' to find relevant entities."
            "</memory-hint>"
        )

    if parts:
        ctx.data.setdefault("memory_context", "\n\n".join(parts))


@hook(event="agent:post_respond", priority=100)
async def bio_memory_post_respond(ctx: Any) -> None:
    """Track messages; trigger periodic consolidation every N turns.

    Only tracks human interactive sessions (those with a session_id).
    Skips automated runs like pulse checks and scheduler tasks to
    prevent transient background noise from polluting memory.
    """
    session_id = ctx.data.get("session_id", "")
    if not session_id:
        return

    messages = ctx.data.get("messages", [])
    if not messages:
        return

    st = _runtime.state()
    st.messages.extend(messages)
    st.turn_count += 1

    interval = st.config.consolidation_interval_turns
    if interval > 0 and st.turn_count % interval == 0:
        model = _eval_model()
        if model is not None:
            all_messages = list(st.messages)
            st.messages.clear()
            # configure() always sets a semaphore; guard for mypy
            semaphore = st.semaphore
            if semaphore is None:
                raise RuntimeError("bio_memory module semaphore not configured")
            spawn_background(
                st.consolidator.periodic_consolidate(all_messages, model),
                background_tasks=st.background_tasks,
                semaphore=semaphore,
                eval_config=st.eval_config,
                telemetry=st.telemetry,
                audit_event_name="bio_memory.consolidation_error",
                logger=_logger,
            )


@hook(event="agent:pre_tool", priority=10)
async def bio_memory_pre_tool(ctx: Any) -> None:
    """Veto bash commands targeting memory paths."""
    tool_name = ctx.data.get("tool", "")
    if tool_name != "bash":
        return

    st = _runtime.state()
    cmd = ctx.data.get("args", {}).get("command", "")
    if _bash_targets_memory(cmd, st):
        ctx.veto("Memory files must be modified via memory tools, not bash.")


@hook(event="agent:post_tool", priority=100)
async def bio_memory_post_tool(ctx: Any) -> None:
    """Emit audit event when a write/edit tool modifies a memory file."""
    tool_name = ctx.data.get("tool", "")
    if tool_name not in ("write", "edit"):
        return

    file_path = ctx.data.get("args", {}).get("file_path", "")
    if not file_path:
        return

    st = _runtime.state()
    try:
        resolved = Path(file_path).resolve()
        if _is_memory_path(resolved, st) and st.telemetry is not None:
            st.telemetry.audit_event(
                "memory.file_modified_by_tool",
                details={"tool": tool_name, "path": str(resolved)},
            )
    except (ValueError, OSError):
        pass


@hook(event="agent:shutdown", priority=100)
async def bio_memory_shutdown(ctx: Any) -> None:
    """Final consolidation safety-net on session end; drain background tasks."""
    del ctx  # event payload unused
    st = _runtime.state()

    if st.config.light_on_shutdown and st.messages:
        model = _eval_model()
        if model is None:
            _logger.warning("No eval model available, skipping final consolidation")
        else:
            semaphore = st.semaphore
            if semaphore is None:
                raise RuntimeError("bio_memory module semaphore not configured")
            spawn_background(
                st.consolidator.periodic_consolidate(st.messages, model),
                background_tasks=st.background_tasks,
                semaphore=semaphore,
                eval_config=st.eval_config,
                telemetry=st.telemetry,
                audit_event_name="bio_memory.consolidation_error",
                logger=_logger,
            )

    if st.background_tasks:
        _logger.info(
            "Awaiting %d bio_memory background task(s) before shutdown",
            len(st.background_tasks),
        )
        await asyncio.gather(*st.background_tasks, return_exceptions=True)


# --- Tools -----------------------------------------------------------------


@tool(
    name="memory_search",
    description=(
        "Search agent memory and team shared knowledge using grep "
        "+ wiki-link graph traversal. Use scope='team' to search "
        "only team shared knowledge."
    ),
    classification="read_only",
    when_to_use="Recall past conversations, facts, entities, or stored context.",
)
async def memory_search(
    query: str,
    scope: str | None = None,
    top_k: int = 5,
) -> str:
    """Search memory across episodes, daily notes, working memory, and entities.

    Results are wrapped in ``<memory-result>`` boundary markers to
    prevent stored content from being interpreted as instructions
    (LLM-01 prompt-injection mitigation).
    """
    st = _runtime.state()
    results = await st.retriever.search(query=query, top_k=top_k, scope=scope)
    if st.telemetry is not None:
        st.telemetry.audit_event(
            "memory.searched",
            details={"query": query[:200], "scope": scope, "results": len(results)},
        )
    if not results:
        return "No memory results found."
    parts = [
        f'<memory-result source="{r.source}" score="{r.score:.2f}">\n{r.content}\n</memory-result>'
        for r in results
    ]
    return "\n".join(parts)


@tool(
    name="memory_note",
    description="Record a note — appends to working memory or creates an episode.",
    classification="state_modifying",
    when_to_use="Persist an observation, decision, or outcome from the current session.",
)
async def memory_note(
    content: str,
    target: str = "working",
) -> str:
    """Write a note to working memory or create a new episode file."""
    from arcagent.utils.sanitizer import sanitize_text

    st = _runtime.state()
    clean = sanitize_text(content, max_length=2000)

    if target == "working":
        await st.working.write(content=clean, frontmatter={"type": "note"})
        if st.telemetry is not None:
            st.telemetry.audit_event(
                "memory.note_written",
                details={"target": "working", "length": len(clean)},
            )
        return "Note recorded to working memory."

    if target == "episode":
        import yaml

        from arcagent.modules.bio_memory.entity_helpers import today_str
        from arcagent.utils.io import atomic_write_text
        from arcagent.utils.sanitizer import slugify

        timestamp = today_str()
        slug = slugify(clean[:50])
        filename = f"{timestamp}-{slug}.md"

        episodes_dir = st.memory_dir / st.config.episodes_dirname
        episodes_dir.mkdir(parents=True, exist_ok=True)
        target_path = episodes_dir / filename

        frontmatter = {"title": slug, "date": timestamp, "tags": ["manual-note"]}
        fm_text = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False).strip()
        episode_content = f"---\n{fm_text}\n---\n\n{clean}\n"
        atomic_write_text(target_path, episode_content)

        if st.telemetry is not None:
            st.telemetry.audit_event(
                "memory.note_written",
                details={"target": "episode", "episode_name": filename},
            )
        return f"Episode created: {filename}"

    return f"Unknown target: {target}"


@tool(
    name="memory_recall",
    description="Recall a specific memory by name (episode or entity).",
    classification="read_only",
    when_to_use="Look up a named episode or entity file directly.",
)
async def memory_recall(name: str) -> str:
    """Recall a named memory and return its content wrapped in boundary markers."""
    st = _runtime.state()
    result = await st.retriever.recall(name)
    if st.telemetry is not None:
        st.telemetry.audit_event(
            "memory.recalled",
            details={"name": name[:200], "found": result is not None},
        )
    if result is None:
        return f"No memory found for '{name}'."
    return f'<memory-result source="{name}">\n{result}\n</memory-result>'


@tool(
    name="memory_consolidate_deep",
    description=(
        "Trigger deep memory consolidation (entity rewrites, graph analysis, merge detection)."
    ),
    classification="state_modifying",
    when_to_use="Run a deep consolidation pass to clean up and link entities.",
)
async def memory_consolidate_deep(dry_run: bool = False) -> str:
    """Trigger deep memory consolidation; returns a human-readable summary."""
    # Lazy import to avoid circular deps at module level
    from arcagent.modules.bio_memory.deep_consolidator import DeepConsolidator

    st = _runtime.state()
    model = _eval_model()
    if model is None:
        return "Deep consolidation unavailable: no eval model configured."

    deep = DeepConsolidator(
        memory_dir=st.memory_dir,
        workspace=st.workspace,
        config=st.config,
        telemetry=st.telemetry,
        team_service_factory=st.consolidator._team_service_factory,
    )

    result = await deep.consolidate(model, agent_id="self")
    if st.telemetry is not None:
        st.telemetry.audit_event("memory.deep_consolidated", details=result)

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
            f"  Stale: {stale.get('flagged', 0)} flagged, {stale.get('archived', 0)} archived"
        )
    return "\n".join(parts)


__all__ = [
    "bio_memory_assemble_prompt",
    "bio_memory_post_respond",
    "bio_memory_post_tool",
    "bio_memory_pre_tool",
    "bio_memory_shutdown",
    "memory_consolidate_deep",
    "memory_note",
    "memory_recall",
    "memory_search",
]
