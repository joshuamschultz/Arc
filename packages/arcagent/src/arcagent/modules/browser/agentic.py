"""Agentic browser tool: ``browser_task``.

A single high-level tool that hands a natural-language goal to a bounded
browser-use agent (an optional extra) instead of driving the page
element-by-element. It is a different shape from the CDP tools — its own
internal loop — so it lives beside them rather than in the backend seam.

Governance:
  - **Federal forbids it** — an opaque, self-directed browser loop whose
    per-step decisions are not individually policy-gated is not
    SCIF-authorizable. Enforced before anything loads.
  - **Off by default**, enabled per agent via
    ``[modules.browser.config.browser_use] enabled = true``.
  - The browser-use dependency and its LLM adapter are imported lazily,
    so a missing extra degrades loudly at call time.
"""

from __future__ import annotations

from arcagent.modules.browser import _runtime
from arcagent.modules.browser.errors import (
    AgenticBrowserForbiddenError,
    BrowserUseNotInstalledError,
    CapabilityDisabledError,
)
from arcagent.tools._decorator import tool


@tool(
    name="browser_task",
    description=(
        "Accomplish a natural-language goal in a real browser (navigate, "
        "read, click, type across multiple steps) via an autonomous "
        "browser agent. Use for multi-step web tasks that the single-step "
        "browser_* tools would make tedious."
    ),
    classification="state_modifying",
    capability_tags=("browser_task",),
    when_to_use=(
        "A web goal needs many coordinated steps (search, open, filter, "
        "extract) and you'd rather state the goal than script each click."
    ),
)
async def browser_task(goal: str) -> str:
    """Run a bounded agentic browser task and return its result."""
    cfg = _runtime.state().config
    if cfg.tier == "federal":
        raise AgenticBrowserForbiddenError(cfg.tier)
    if not cfg.browser_use.enabled:
        raise CapabilityDisabledError("browser_task")

    try:
        from arcagent.modules.browser._browser_use.runner import run_browser_task
    except ImportError as exc:
        raise BrowserUseNotInstalledError() from exc

    result = await run_browser_task(goal, cfg.browser_use)

    bus = _runtime.state().bus
    if bus is not None:
        await bus.emit("browser.task_run", {"max_steps": cfg.browser_use.max_steps})
    return f"[EXTERNAL WEB CONTENT] {result}"
