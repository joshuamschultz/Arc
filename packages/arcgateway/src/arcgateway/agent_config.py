"""Agent config helpers — read optional sections from an agent's ``arcagent.toml``.

Distinct from :mod:`arcgateway.config`, which is the Pydantic model for the
gateway daemon's own ``gateway.toml``. This module reads *agent* config (the
file that lives at ``team/<agent>/arcagent.toml``) and surfaces presentation
hints to arcui via the gateway data plane.

Currently exposes only the optional ``[ui]`` block (PRD §F17). The agent
self-describes its display hints — keeping the single-source-of-truth invariant
intact (D-003).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UISection:
    """Parsed ``[ui]`` block from an agent's ``arcagent.toml``.

    All fields are optional. Defaults are conservative: ``hidden=False`` (agent
    surfaces in fleet view), and ``None`` for unset display hints (callers
    fall back to deterministic alternatives — e.g. hashed color from agent_id).
    """

    display_name: str | None
    color: str | None
    role_label: str | None
    hidden: bool


def load_ui_section(toml_dict: dict[str, Any]) -> UISection:
    """Read the optional ``[ui]`` section from a parsed ``arcagent.toml`` dict.

    Returns an all-default :class:`UISection` if ``[ui]`` is absent, malformed
    (non-dict), or contains values of unexpected types. Strict typing —
    non-string ``display_name`` / ``color`` / ``role_label`` are dropped to
    ``None`` rather than coerced, and ``hidden`` accepts only Python ``bool``.

    Args:
        toml_dict: Result of ``tomllib.loads(arcagent_toml_text)``.

    Returns:
        :class:`UISection` with defaults filled in.
    """
    raw = toml_dict.get("ui")
    if not isinstance(raw, dict):
        return _defaults()

    return UISection(
        display_name=_str_or_none(raw.get("display_name")),
        color=_str_or_none(raw.get("color")),
        role_label=_str_or_none(raw.get("role_label")),
        hidden=raw.get("hidden") is True,
    )


def _defaults() -> UISection:
    return UISection(
        display_name=None,
        color=None,
        role_label=None,
        hidden=False,
    )


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) else None
