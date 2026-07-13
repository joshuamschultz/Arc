"""Full arcllm option surface for generated ``arcllm.toml`` files.

The complete ``[modules.*]`` surface is DERIVED from arcllm's own packaged
``config.toml`` at generation time — the single source of truth — so a new
module (or a changed default/note) shows up in every generated file without a
second edit here. Both the ``arc init`` (fleet ``~/.arc/arcllm.toml``) and the
agent scaffold (``team/<agent>/arcllm.toml``) generators render from this.

Rendering rule (the "commented at default" operator choice): each module block
is emitted VERBATIM from ``config.toml`` — notes and all — with every setting
line commented out at its default. Uncommenting a line is what makes it an
active override of the layer below; until then it is pure documentation and
changes nothing. The fleet generator additionally emits its tier-preset modules
ACTIVE (that is its canonical layer) and only renders the REMAINING modules
through here for discoverability.
"""

from __future__ import annotations

import importlib.resources as ir
import re
from collections.abc import Iterable

# The cheaper background/eval model arcagent composes from ``[eval]``. One
# canonical block shared by both generators so the fleet and per-agent files
# never drift on which eval knobs exist.
EVAL_BLOCK: tuple[str, ...] = (
    "[eval]",
    "# The cheaper background/eval model (entity extraction, policy eval, compaction,",
    "# memory consolidation). Empty provider/model fall back to the agent's own.",
    'provider = ""              # empty = agent\'s provider',
    'model = ""                 # empty = agent\'s model',
    "max_tokens = 1024          # eval output cap",
    "max_input_tokens = 100000  # per-eval input budget (over-budget = chunked; 0 = unlimited)",
    "temperature = 0.2          # low for evaluation consistency",
    "timeout_seconds = 30       # per eval call",
    'fallback_behavior = "skip"  # skip | error',
    "max_concurrent = 2         # eval semaphore limit",
    "background_queue_size = 10   # per-module background task queue depth",
    "background_task_timeout = 120  # seconds before a background task times out",
)

# Per-run LLM consumption ceilings (LLM10). Commented = unbounded at personal;
# enterprise/federal treat a set value as a non-relaxable floor.
BUDGET_BLOCK: tuple[str, ...] = (
    "[budget]",
    "# max_tokens =      # per-run token ceiling (unset = unbounded at personal)",
    "# max_cost_usd =    # per-run cost ceiling",
    "# max_requests =    # per-run request ceiling",
)


def _packaged_config_text() -> str:
    """Raw text of arcllm's packaged ``config.toml`` (the module source of truth)."""
    return (ir.files("arcllm") / "config.toml").read_text(encoding="utf-8")


def _module_top_name(block: str) -> str | None:
    """Top-level module name of a ``[modules.<name>...]`` block, or None."""
    match = re.search(r"\[modules\.([a-z_]+)", block)
    return match.group(1) if match else None


def commented_module_surface(*, exclude: Iterable[str] = (), prefix: str = "") -> str:
    """Render every arcllm module block commented at its packaged default.

    Args:
        exclude: Top-level module names to skip (the fleet generator excludes
            the modules it already emits ACTIVE from its tier preset).
        prefix: Table-path prefix — ``""`` for the fleet file (``[modules.x]``,
            read by arcllm directly) or ``"llm."`` for a per-agent file
            (``[llm.modules.x]``, the arcagent override path).
    """
    skip = set(exclude)
    rendered: list[str] = []
    for block in re.split(r"\n\s*\n", _packaged_config_text()):
        name = _module_top_name(block)
        if name is None or name in skip:
            continue
        lines = []
        for line in block.splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                line = "# " + line
            if prefix:
                line = line.replace("[modules.", f"[{prefix}modules.")
            lines.append(line)
        rendered.append("\n".join(lines))
    return "\n\n".join(rendered)


__all__ = ["BUDGET_BLOCK", "EVAL_BLOCK", "commented_module_surface"]
