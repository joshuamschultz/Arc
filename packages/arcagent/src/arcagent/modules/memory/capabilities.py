"""Decorator-form memory module — SPEC-021 task 3.1.

Six ``@hook`` functions mirror :class:`MarkdownMemoryModule`'s
``startup`` registrations plus the Module-protocol shutdown:

  * ``agent:assemble_prompt`` (priority 50) — inject notes + guidance.
  * ``agent:pre_tool``        (priority 10) — bash deny + path routing.
  * ``agent:post_tool``       (priority 100) — identity audit capture.
  * ``agent:post_respond``    (priority 100) — cheap per-turn raw note
    append (SPEC-030 Tier 1); stage messages for background extraction;
    ensure today's notes file exists (which lazily rolls up the prior day).
  * ``agent:shutdown``        (priority 100) — drain background tasks,
    consolidate today's notes (Tier 2b), close hybrid-search resources.

Daily notes are ongoing memory, triggered by turns / session-end / new-day —
never by context compaction (SPEC-030 D-408: coupling memory to token
pressure is the MemGPT anti-pattern sleep-time compute exists to undo).

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
import re
import shlex
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from arcllm.types import Message

from arcagent.modules.memory import _runtime
from arcagent.modules.memory.markdown_memory import _MEMORY_SUBPATHS
from arcagent.tools._decorator import background_task, hook, tool
from arcagent.utils.model_helpers import get_eval_model, spawn_background
from arcagent.utils.sanitizer import sanitize_text

_logger = logging.getLogger("arcagent.modules.memory.capabilities")

# Long-term memory sink: day rollups append here; get_recent_notes surfaces it
# so consolidated memory is recallable. Not a dated file (skipped by rollup scan).
_LONGTERM_NOTES = "_longterm.md"

# Max chars written from a single eval-model consolidation/rollup pass.
_NOTE_SANITIZE_MAX_CHARS = 8000

_CONSOLIDATE_PROMPT = (
    "Dedupe and tidy these notes into a flat bulleted list. Preserve every "
    "distinct fact, decision, and open item; drop repetition and filler. Do "
    "not invent anything. Output only the bulleted list.\n\nNOTES:\n"
)

_ROLLUP_PROMPT = (
    "Consolidate a day's notes into a concise, durable summary for long-term "
    "recall. Keep distinct facts, decisions, outcomes, and unresolved items as "
    "brief bullets; drop chatter and timestamps. Do not invent. Output only the "
    "summary.\n\nDAY NOTES:\n"
)

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


def _today() -> date:
    """Current UTC date — used for both file names and day-boundary logic so the
    ``Z``-stamped raw-append times never disagree with the file's date."""
    return datetime.now(UTC).date()


def _ensure_daily_notes(extra_content: str = "") -> None:
    """Create today's notes file if missing; lazily roll up the prior day.

    Creating today's file for the first time is the new-day boundary — it fires
    for a daemon crossing midnight and for a next-day one-shot run alike, with no
    live scheduler (SPEC-030 Tier 3).
    """
    workspace = _runtime.state().workspace
    today = _today()
    notes_file = workspace / "notes" / f"{today.isoformat()}.md"
    if notes_file.exists():
        return
    notes_file.parent.mkdir(parents=True, exist_ok=True)
    content = f"# Daily Notes - {today.isoformat()}\n\n"
    if extra_content:
        content += extra_content
    notes_file.write_text(content, encoding="utf-8")
    _logger.debug("Auto-created daily notes file: %s", notes_file.name)
    _maybe_rollup_previous_day(today)


# --- SPEC-030: ongoing daily notes helpers -------------------------------


def _excerpt(text: str, limit: int = 120) -> str:
    """One-line excerpt: collapse whitespace, cap length. No LLM."""
    return " ".join(text.split())[:limit]


def _last_content(messages: list[dict[str, Any]], role: str) -> str:
    """Latest string content for a role in the staged turn."""
    for msg in reversed(messages):
        if msg.get("role") == role:
            content = msg.get("content", "")
            return content if isinstance(content, str) else ""
    return ""


def _append_raw_note(messages: list[dict[str, Any]]) -> None:
    """Tier 1: append one plain line for this turn — crash-safety, no LLM.

    The durable record if the process dies before the background extractor
    runs or before session-end consolidation ("ASSUME INTERRUPTION"). Direct
    owner-driven append (the append-only veto guards tool writes, not this).
    """
    st = _runtime.state()
    if not st.config.raw_capture_enabled:
        return
    notes_file = st.workspace / "notes" / f"{_today().isoformat()}.md"
    # Sanitize each excerpt: this raw record can be re-injected next turn (and as
    # tomorrow's "### Yesterday") before any consolidation runs (ASI-06).
    user = sanitize_text(_excerpt(_last_content(messages, "user")), max_length=120)
    assistant = sanitize_text(_excerpt(_last_content(messages, "assistant")), max_length=120)
    line = f"- {datetime.now(UTC).strftime('%H:%M:%SZ')} · user: {user} → assistant: {assistant}\n"
    try:
        with notes_file.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:  # reason: fail-open — a raw-capture IO error must not abort the turn
        _logger.warning("Raw note append failed", exc_info=True)


def _sanitize_note_output(text: str) -> str:
    """Sanitize eval-model output before it is written to a notes file (ASI-06)."""
    return sanitize_text(
        text, max_length=_NOTE_SANITIZE_MAX_CHARS, truncation_suffix="\n[truncated]"
    )


async def _consolidate_today_notes() -> None:
    """Tier 2b: one eval-model dedupe/tidy pass over today's notes at session end.

    The guaranteed consolidation floor for short/one-shot runs. Fail-open: on
    any error the raw appends are left intact and shutdown proceeds.
    """
    st = _runtime.state()
    if not st.config.session_consolidation_enabled:
        return
    today = _today()
    notes_file = st.workspace / "notes" / f"{today.isoformat()}.md"
    if not notes_file.exists():
        return
    content = notes_file.read_text(encoding="utf-8")
    if not content.strip():
        return
    model = _eval_model()
    if model is None:
        return
    try:
        response = await model.invoke(
            [Message(role="user", content=_CONSOLIDATE_PROMPT + content)]
        )
        tidied = response.content
        if not tidied or not tidied.strip():
            return
        header = f"# Daily Notes - {today.isoformat()}\n\n"
        notes_file.write_text(header + _sanitize_note_output(tidied) + "\n", encoding="utf-8")
        if st.telemetry is not None:
            # Audit the lossy rewrite: the crash-safe raw record is replaced by an
            # LLM summary; a security-relevant memory mutation must be recorded (AU).
            st.telemetry.audit_event("memory.consolidation", {"day": today.isoformat()})
    except Exception:  # reason: fail-open — keep raw appends; shutdown must complete
        _logger.warning("Session-end notes consolidation failed; keeping raw notes", exc_info=True)
        if st.telemetry is not None:
            st.telemetry.audit_event("memory.consolidation_error", {"day": today.isoformat()})


def _parse_date_stem(stem: str) -> date | None:
    """Parse a ``YYYY-MM-DD`` file stem to a date; None for non-dated files."""
    try:
        return date.fromisoformat(stem)
    except ValueError:
        return None


def _unrolled_prior_days(notes_dir: Path, today: date) -> list[Path]:
    """All ``notes/<date>.md`` with date < today and no ``.rolled`` marker,
    oldest-first. Draining the whole backlog avoids starving older days when a
    sparsely-run agent accumulates several un-rolled days."""
    if not notes_dir.exists():
        return []
    candidates: list[tuple[date, Path]] = []
    for note_file in notes_dir.glob("*.md"):
        day = _parse_date_stem(note_file.stem)
        if day is None or day >= today:
            continue
        if (notes_dir / f"{note_file.stem}.rolled").exists():
            continue
        candidates.append((day, note_file))
    candidates.sort()
    return [path for _, path in candidates]


def _maybe_rollup_previous_day(today: date) -> None:
    """Tier 3: enqueue a background rollup for every un-rolled prior day."""
    st = _runtime.state()
    if not st.config.daily_rollup_enabled:
        return
    prior = _unrolled_prior_days(st.workspace / "notes", today)
    if not prior:
        return
    semaphore = st.semaphore
    if semaphore is None:
        raise RuntimeError("memory module semaphore not configured")
    for prev in prior:
        spawn_background(
            _rollup_previous_day(prev),
            background_tasks=st.background_tasks,
            semaphore=semaphore,
            eval_config=st.eval_config,
            telemetry=st.telemetry,
            audit_event_name="memory.rollup_error",
            logger=_logger,
        )


def _upsert_longterm_section(longterm: Path, date_stem: str, body: str) -> None:
    """Insert-or-replace the ``## <date_stem>`` section in the long-term file.

    Idempotent by date: re-running a rollup for the same day overwrites its
    section rather than appending a duplicate, so a retry after a crash between
    the section write and the ``.rolled`` marker is safe.
    """
    header = f"## {date_stem}"
    existing = longterm.read_text(encoding="utf-8") if longterm.exists() else ""
    kept: list[str] = []
    skipping = False
    for line in existing.splitlines():
        if line.startswith("## "):
            skipping = line.strip() == header
        if not skipping:
            kept.append(line)
    prefix = "\n".join(kept).rstrip()
    # Neutralize markdown headings in the (model-derived) body so it can't forge a
    # ``## <date>`` boundary that corrupts section parsing on a later upsert (ASI-06).
    safe_body = re.sub(r"^(#{1,6})(\s)", r" \1\2", body, flags=re.MULTILINE)
    block = f"{header}\n\n{safe_body}"
    longterm.write_text((f"{prefix}\n\n{block}\n" if prefix else f"{block}\n"), encoding="utf-8")


async def _rollup_previous_day(prev: Path) -> None:
    """Consolidate a prior day into long-term memory; mark rolled (idempotent).

    The long-term write is an upsert keyed by date (``_upsert_longterm_section``)
    and the ``.rolled`` marker is written last. A crash anywhere before the marker
    leaves the day eligible for exactly one retry, and the retry overwrites its own
    ``## <date>`` section rather than appending — so no duplicate survives.
    """
    st = _runtime.state()
    notes_dir = prev.parent
    marker = notes_dir / f"{prev.stem}.rolled"
    if marker.exists():
        return  # race guard — already rolled
    content = prev.read_text(encoding="utf-8") if prev.exists() else ""
    if not content.strip():
        marker.write_text("", encoding="utf-8")  # nothing to roll — mark done
        return
    model = _eval_model()
    if model is None:
        return  # no model — leave un-rolled; retried on the next new-day boundary
    response = await model.invoke([Message(role="user", content=_ROLLUP_PROMPT + content)])
    summary = response.content
    if not summary or not summary.strip():
        return
    sanitized = _sanitize_note_output(summary)
    longterm = notes_dir / _LONGTERM_NOTES
    # Upsert by date so a crash between this write and the marker cannot produce a
    # duplicate ``## <date>`` section on retry — the rollup is a pure function of the
    # (never-mutated) source file, so the retry re-reads identical input.
    _upsert_longterm_section(longterm, prev.stem, sanitized)
    marker.write_text("", encoding="utf-8")  # LAST — write-then-mark
    if st.telemetry is not None:
        st.telemetry.audit_event("memory.rollup", {"day": prev.stem})
    _logger.info("Rolled up prior-day notes: %s", prev.name)


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
    """Ensure today's notes, cheap raw-capture the turn, stage for extraction."""
    st = _runtime.state()
    messages = ctx.data.get("messages", [])
    if not messages:
        return

    _ensure_daily_notes()  # creates today's file (+ lazy prior-day rollup)
    _append_raw_note(messages)  # Tier 1: crash-safe per-turn append (no LLM)

    if st.config.entity_extraction_enabled:
        # Latest pair wins — the background loop drains it.
        st.pending_messages = list(messages)


@hook(event="agent:shutdown", priority=100)
async def memory_shutdown(ctx: Any) -> None:
    """Drain background tasks, consolidate today's notes, close resources.

    Session-end consolidation (SPEC-030 Tier 2b): after in-flight background
    tasks finish, run one eval-model pass to dedupe/tidy today's raw appends.
    This is the guaranteed consolidation floor for short/one-shot runs that
    never hit a new-day rollup boundary. Fail-open — a model error leaves the
    raw appends intact and shutdown still completes.
    """
    del ctx  # event payload unused
    st = _runtime.state()
    if st.background_tasks:
        _logger.info(
            "Awaiting %d memory background task(s) before shutdown",
            len(st.background_tasks),
        )
        await asyncio.gather(*st.background_tasks, return_exceptions=True)
    await _consolidate_today_notes()
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
        except Exception:  # reason: fail-open — log + continue
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
    "memory_pre_tool",
    "memory_search",
    "memory_shutdown",
]
