"""Render the ``<policy>`` prompt section: pinned operator rules + budgeted learned bullets.

Two kinds of text share the policy section and must never be confused:

* **Pinned operator rules** — the operator-signed ``policy_pinned.md`` document.
  The curator has no code path to it, so these rules are never re-scored, rewritten
  or pruned; they always render, in full, ahead of everything else.
* **Learned bullets** — the agent-curated ``policy.md`` playbook. The bookkeeping the
  curator needs (``score``, ``uses``, dates, source) is stripped before the text
  reaches the model, and the bullets are trimmed to ``max_prompt_tokens``,
  highest-scored first, so a long-lived playbook cannot grow the fixed per-call cost
  without bound (J2 F5).

Token sizes use the same ~4 characters per token estimate the trace view shows.
"""

from __future__ import annotations

from arcagent.modules.policy._bullet_parse import parse_bullets

_CHARS_PER_TOKEN = 4
_PINNED_HEADING = "## Operator rules (pinned)"
_LEARNED_HEADING = "## Learned"


def _tokens(text: str) -> int:
    return max(0, round(len(text) / _CHARS_PER_TOKEN))


def _learned_lines(learned_md: str, budget_tokens: int) -> tuple[list[str], int]:
    """Highest-scored learned bullets that fit ``budget_tokens``, and how many were left out."""
    bullets = sorted(parse_bullets(learned_md), key=lambda b: int(b["score"]), reverse=True)
    kept: list[str] = []
    used = _tokens(_LEARNED_HEADING)
    for bullet in bullets:
        line = f"- {bullet['text']}"
        used += _tokens(line) + 1
        if used > budget_tokens:
            break
        kept.append(line)
    return kept, len(bullets) - len(kept)


def render_policy_section(pinned: str, learned_md: str, *, max_tokens: int) -> str:
    """The policy section text: pinned rules in full, then learned bullets within budget."""
    parts: list[str] = []
    pinned = pinned.strip()
    if pinned:
        parts.append(f"{_PINNED_HEADING}\n{pinned}")
    remaining = max_tokens - sum(_tokens(p) for p in parts)
    lines, omitted = _learned_lines(learned_md, remaining)
    if lines:
        parts.append(f"{_LEARNED_HEADING}\n" + "\n".join(lines))
    if omitted:
        parts.append(
            f"({omitted} lower-scored learned rules omitted: the policy section is capped "
            f"at {max_tokens} tokens)"
        )
    return "\n\n".join(parts)


__all__ = ["render_policy_section"]
