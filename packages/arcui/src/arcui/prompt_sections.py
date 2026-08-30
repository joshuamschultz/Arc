"""Ordered, labeled prompt-section projection for the LLM call detail view (H-049).

The persisted ``request_body`` (arcllm telemetry, ``modules/telemetry.py``)
carries the exact messages sent to the model. arcagent assembles the system
prompt as a sequence of top-level XML-ish elements — ``<base>``, ``<identity>``,
``<policy>``, ``<capabilities>`` (which nests ``<available-tools>`` /
``<available-skills>``), ``<context>``, several ``<strategy_*>`` /
``<*_guidance>`` blocks, plus module sections — and attaches the turn's
retrieved material inside ``<agent-context>`` on the user message (see
``arcagent/core/session_internal/context.py``). Those bytes are ordered for
provider-cache stability (most-stable prefix first, then alphabetical), NOT for
a reader.

This module is a READ-ONLY projection: it parses what was actually sent and
returns the sections in the operator-legible order H-049 asks for —
system prompt · identity · strategies · policies · tool list · skill list ·
context · session data — with every remaining real section preserved after,
never dropped and never fabricated. It changes no wire bytes and adds nothing
that was not in the request.

arcui does not deep-import arcagent internals (layering); the tag names below
mirror that assembler and are kept honest by the tests, not a shared import.
"""

from __future__ import annotations

import re
from typing import Any

# A top-level section as arcagent's ``_render`` emits it: ``<tag>\nbody\n</tag>``.
# The backreference keeps a whole ``<capabilities>…</capabilities>`` region
# (which nests other, differently-named tags) captured as one unit.
_TOP_SECTION_RE = re.compile(r"<([a-zA-Z][\w-]*)>\n([\s\S]*?)\n</\1>")
_NESTED_TOOLS_RE = re.compile(r"<available-tools>\s*([\s\S]*?)\s*</available-tools>")
_NESTED_SKILLS_RE = re.compile(r"<available-skills>\s*([\s\S]*?)\s*</available-skills>")
_AGENT_CONTEXT_RE = re.compile(r"<agent-context>\s*([\s\S]*?)\s*</agent-context>")

# Tags consumed by a named canonical bucket, so they are not re-emitted as extras.
_BASE_TAG = "base"
_IDENTITY_TAG = "identity"
_POLICY_TAG = "policy"
_CONTEXT_TAG = "context"
_CAPABILITIES_TAG = "capabilities"


def _approx_tokens(text: str) -> int:
    """Rough token count (~4 chars/token) — a size hint before opening a section."""
    return max(0, round(len(text) / 4))


def _is_strategy_tag(tag: str) -> bool:
    """arcrun's strategy guidance lands as several sibling tags — ``strategy_react``,
    ``strategy_selection``, ``code_exec_guidance``, ``spawn_guidance`` — rather than
    one ``<strategies>`` blob. Group them under the one display section."""
    return tag.startswith("strategy") or tag.endswith("_guidance")


def _prettify(tag: str) -> str:
    """``memory_status`` / ``workflow-node`` → ``Memory Status`` / ``Workflow Node``."""
    return tag.replace("-", " ").replace("_", " ").strip().title()


def _collect_system_text(messages: list[Any]) -> str:
    """Concatenate every ``role:"system"`` message's string content.

    arcagent hands arcrun a two-segment system prompt (session-stable +
    run-stable cache tiers), each its own system message; joining them recovers
    the full assembled prompt for parsing.
    """
    parts: list[str] = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                parts.append(content)
    return "\n\n".join(parts)


def _collect_agent_context(messages: list[Any]) -> str:
    """The turn's retrieved material — every ``<agent-context>`` block on a
    non-system message (arcagent attaches it to the user turn)."""
    blocks: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") == "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            blocks.extend(m.group(1).strip() for m in _AGENT_CONTEXT_RE.finditer(content))
    return "\n\n".join(b for b in blocks if b)


def _parse_top_sections(system_text: str) -> list[tuple[str, str]]:
    """Top-level ``(tag, body)`` pairs in source (wire) order."""
    return [(m.group(1), m.group(2).strip()) for m in _TOP_SECTION_RE.finditer(system_text)]


def _tool_list_from_request_tools(tools: Any) -> str:
    """A readable tool list built from the request's tool schemas — the fallback
    when the system prompt carried no ``<available-tools>`` manifest.

    Handles the Anthropic shape (``{"name", "description", ...}``) and the
    OpenAI function shape (``{"type":"function","function":{"name", ...}}``).
    """
    if not isinstance(tools, list):
        return ""
    lines: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        spec: dict[str, Any] = fn if isinstance(fn, dict) else tool
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            continue
        desc = spec.get("description")
        lines.append(f"{name} — {desc}" if isinstance(desc, str) and desc else name)
    return "\n".join(lines)


def _section(key: str, label: str, body: str) -> dict[str, Any]:
    return {"key": key, "label": label, "body": body.strip(), "tokens": _approx_tokens(body)}


def build_prompt_sections(request_body: Any) -> list[dict[str, Any]]:
    """Ordered, labeled prompt sections for one persisted LLM request.

    Returns ``[]`` when there is nothing to show (no body, metadata-only /
    federal-encrypted default), so the caller degrades to its plain view.
    """
    if not isinstance(request_body, dict):
        return []
    raw_messages = request_body.get("messages")
    messages: list[Any] = raw_messages if isinstance(raw_messages, list) else []
    system_text = _collect_system_text(messages)
    session_data = _collect_agent_context(messages)
    parsed = _parse_top_sections(system_text)
    by_tag = {tag: body for tag, body in parsed}

    # Pull the tool/skill manifests out of the capabilities umbrella so each
    # reads as its own section; keep any leftover capabilities prose.
    caps_body = by_tag.get(_CAPABILITIES_TAG, "")
    tools_body = ""
    skills_body = ""
    caps_remainder = caps_body
    if caps_body:
        tool_match = _NESTED_TOOLS_RE.search(caps_body)
        skill_match = _NESTED_SKILLS_RE.search(caps_body)
        tools_body = tool_match.group(1).strip() if tool_match else ""
        skills_body = skill_match.group(1).strip() if skill_match else ""
        caps_remainder = _NESTED_SKILLS_RE.sub("", _NESTED_TOOLS_RE.sub("", caps_body)).strip()
    if not tools_body:
        tools_body = _tool_list_from_request_tools(request_body.get("tools"))

    # Strategy guidance is several sibling tags — group them, keeping each block
    # tagged so the renderer can still show them as nested parts.
    strategies_body = "\n\n".join(
        f"<{tag}>\n{body}\n</{tag}>" for tag, body in parsed if _is_strategy_tag(tag) and body
    )

    ordered: list[dict[str, Any]] = []

    def add(key: str, label: str, body: str) -> None:
        if body and body.strip():
            ordered.append(_section(key, label, body))

    # --- canonical H-049 order --------------------------------------------
    add("system_prompt", "System prompt", by_tag.get(_BASE_TAG, ""))
    add("identity", "Identity", by_tag.get(_IDENTITY_TAG, ""))
    add("strategies", "Strategies", strategies_body)
    add("policies", "Policies", by_tag.get(_POLICY_TAG, ""))
    add("tool_list", "Tool list", tools_body)
    add("skill_list", "Skill list", skills_body)
    add("context", "Context", by_tag.get(_CONTEXT_TAG, ""))
    add("session_data", "Session data", session_data)

    # --- every other real section, in source order, never dropped ----------
    consumed = {_BASE_TAG, _IDENTITY_TAG, _POLICY_TAG, _CONTEXT_TAG, _CAPABILITIES_TAG}
    add("capabilities", "Capabilities", caps_remainder)
    for tag, body in parsed:
        if tag in consumed or _is_strategy_tag(tag):
            continue
        add(tag, _prettify(tag), body)

    return ordered
