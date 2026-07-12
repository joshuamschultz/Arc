"""Input curation — keep substantive content, drop mechanical tool plumbing.

Distillation should learn from the conversation (user turns, agent responses,
observations) AND from the knowledge the agent gathers or creates (web search,
research, retrieval, file writes) — but NOT from the high-volume mechanical frames
a tool loop emits (argument echoes, short status/ack results, retries, traces).
Those inflate the distiller's input (more tokens, more chunks) and pollute the
extracted entities/insights with plumbing.

A ``tool`` event survives if ANY keep gate fires: it references a real entity, it
is a knowledge-producing tool (``cfg.curate_keep_tools``), its result is
substantive by length, or it clears the salience floor. Only short mechanical
frames like ``tool:read -> ok`` are stripped. The tool name is read from the
capture convention ``tool:<name> -> <result>``; an unrecognized shape simply falls
through to the length/entity/salience gates.

This filter is **pure and deterministic** — it reuses the entity tags already set
at capture time (``Event.entities``) and issues NO LLM or embedding call. It runs
upstream of chunking, so dropping plumbing here also shrinks the token budget the
distiller must chunk over. Order and ``event_id`` are preserved so downstream
citations stay intact. Toggle it off (``cfg.curate_input=False``) for the
identity function.
"""

from __future__ import annotations

from arcmemory.config import MemoryConfig
from arcmemory.types import Event

_TOOL_KIND = "tool"
_TOOL_PREFIX = "tool:"


def _tool_name(text: str) -> str:
    """Extract ``<name>`` from a captured ``tool:<name> -> <result>`` frame ('' if absent)."""
    if not text.startswith(_TOOL_PREFIX):
        return ""
    return text[len(_TOOL_PREFIX) :].split("->", 1)[0].split(" ", 1)[0].strip()


def _keep_tool(event: Event, cfg: MemoryConfig) -> bool:
    """Whether one ``tool`` event carries enough signal to survive curation.

    Kept when it references a real entity (the capture tagger found one — plumbing
    tags nothing, domain content does), is a knowledge-producing tool, carries a
    substantive-length result, or clears a configured salience floor. Otherwise it
    survives only when entity-gating is turned off entirely.
    """
    if event.entities:
        return True
    if _tool_name(event.text) in cfg.curate_keep_tools:
        return True
    if len(event.text) >= cfg.curate_min_substantive_chars:
        return True
    if cfg.curate_tool_keep_salience > 0.0 and event.salience >= cfg.curate_tool_keep_salience:
        return True
    return not cfg.curate_tool_requires_entity


def curate_for_distillation(events: list[Event], cfg: MemoryConfig) -> list[Event]:
    """Return the events worth distilling: conversation always, tools if meaningful.

    Non-``tool`` kinds (user, respond, observation, action, …) are always kept.
    ``tool`` events pass only ``_keep_tool``. Preserves order + ``event_id``.
    """
    if not cfg.curate_input:
        return events
    return [e for e in events if e.kind != _TOOL_KIND or _keep_tool(e, cfg)]


__all__ = ["curate_for_distillation"]
