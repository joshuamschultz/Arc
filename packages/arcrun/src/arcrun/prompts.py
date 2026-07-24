"""Strategy prompt provider — model-facing guidance for ArcRun capabilities.

ArcRun owns the strategies and their documentation. Consuming agents
(e.g. ArcAgent) call get_strategy_prompts() to obtain prompt fragments
they inject into the system prompt. This keeps separation clean:

- ArcRun knows WHAT strategies do and WHEN to use them
- The consuming agent owns prompt ASSEMBLY and decides what goes in

ArcRun does NOT carry tool-specific guidance (spawn_task, delegate, etc).
Tool guidance lives with the tool's owner — e.g. spawn_task guidance is
in ``arcagent.orchestration.prompts``.

See ADR on strategy prompt injection for architectural rationale.
"""

from __future__ import annotations

from arcprompt import PromptMissing, PromptResolve, load_stock

from arcrun.strategies import STRATEGIES, _load_strategies


def get_strategy_prompts(
    *,
    allowed_strategies: list[str] | None = None,
    tool_names: list[str] | None = None,
    resolve: PromptResolve = load_stock,
) -> dict[str, str]:
    """Return prompt guidance fragments keyed by section name.

    Consuming agents call this to obtain model-facing text that teaches
    the LLM when and how to use ArcRun's capabilities. The returned
    dict is meant to be injected into the system prompt.

    Args:
        allowed_strategies: Which strategies are available. None means
            only 'react' (the default). If multiple are listed, strategy
            selection guidance is included.
        tool_names: Names of tools that will be available. Used to
            detect arcrun-owned tools (execute_python, contained_execute_python)
            and include their guidance.
        resolve: Prompt-body resolver ``(package, name) -> body``. Defaults to
            stock-only ``load_stock``; the agent passes a run-frozen,
            overlay-aware snapshot resolver so an operator's override of a
            strategy prompt is honored here (and audited in the provenance event).

    Returns:
        Dict mapping section names to prompt text. Keys are stable
        identifiers (e.g. 'strategy_react', 'code_exec_guidance') that
        the consuming agent can use for ordering and caching. Tool
        guidance for non-arcrun tools (e.g. spawn_task) is the
        responsibility of those tools' owners.
    """
    if not STRATEGIES:
        _load_strategies()

    sections: dict[str, str] = {}
    effective_tools = tool_names or []

    # --- Core loop behavior (from active strategy) ---
    effective_strategies = allowed_strategies or ["react"]
    for name in effective_strategies:
        strategy = STRATEGIES.get(name)
        if strategy is not None:
            sections[f"strategy_{name}"] = _strategy_body(name, strategy.prompt_guidance, resolve)

    # --- Strategy selection guidance (when multiple available) ---
    if len(effective_strategies) > 1:
        lines = []
        for name in effective_strategies:
            strategy = STRATEGIES.get(name)
            if strategy is not None:
                desc = _strategy_body(f"{name}_description", strategy.description, resolve)
                lines.append(f"- **{name}**: {desc}")
        descriptions = "\n".join(lines)
        sections["strategy_selection"] = (
            "## Strategy Selection\n"
            "Multiple execution strategies are available. The system "
            "will select the best one based on your task, but "
            "understanding them helps you work effectively:\n\n"
            f"{descriptions}"
        )

    # --- Code execution guidance (arcrun-owned tools only) ---
    if "execute_python" in effective_tools:
        sections["code_exec_guidance"] = resolve("arcrun", "code_exec_guidance")
    if "contained_execute_python" in effective_tools:
        sections["contained_exec_guidance"] = resolve("arcrun", "contained_exec_guidance")

    return sections


def _strategy_body(prompt_name: str, fallback: str, resolve: PromptResolve) -> str:
    """Resolve ``strategy_<prompt_name>`` through ``resolve``; fall back for a custom strategy.

    Built-in strategies each ship a ``strategy_<name>[_description]`` prompt, so
    ``resolve`` (stock or overlay-aware) returns their body. A third-party
    strategy that ships no such prompt falls back to its in-class property, so a
    custom strategy is never broken by the externalization.
    """
    try:
        return resolve("arcrun", f"strategy_{prompt_name}")
    except PromptMissing:
        return fallback
