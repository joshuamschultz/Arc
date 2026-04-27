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

from arcrun.strategies import STRATEGIES, _load_strategies

# ---------------------------------------------------------------------------
# Builtin tool guidance constants
# ---------------------------------------------------------------------------

CODE_EXEC_GUIDANCE = """\
## Code Execution (execute_python)
You have access to a sandboxed Python execution environment. Use it when
the problem is more naturally solved by writing code than by calling
predefined tools.

Prefer execute_python when:
- The task involves computation, math, or data transformation
- You need to process structured data (parse JSON, CSV, etc.)
- Logic is complex enough that reasoning alone is error-prone
- You need to verify a hypothesis empirically

Prefer other tools when:
- A dedicated tool already handles the operation (file read/write, search)
- The task requires external API access or credentials
- The operation is security-sensitive or irreversible"""

CONTAINED_EXEC_GUIDANCE = """\
## Isolated Code Execution (contained_execute_python)
You have access to a container-isolated Python execution environment.
It runs with no network access, a read-only filesystem, and strict
memory/CPU/PID limits. Use it for the same scenarios as execute_python
but when stronger isolation is required — untrusted input processing,
resource-intensive computation, or when the execution environment must
not affect the host."""


def get_strategy_prompts(
    *,
    allowed_strategies: list[str] | None = None,
    tool_names: list[str] | None = None,
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
            sections[f"strategy_{name}"] = strategy.prompt_guidance

    # --- Strategy selection guidance (when multiple available) ---
    if len(effective_strategies) > 1:
        descriptions = "\n".join(
            f"- **{name}**: {STRATEGIES[name].description}"
            for name in effective_strategies
            if name in STRATEGIES
        )
        sections["strategy_selection"] = (
            "## Strategy Selection\n"
            "Multiple execution strategies are available. The system "
            "will select the best one based on your task, but "
            "understanding them helps you work effectively:\n\n"
            f"{descriptions}"
        )

    # --- Code execution guidance (arcrun-owned tools only) ---
    if "execute_python" in effective_tools:
        sections["code_exec_guidance"] = CODE_EXEC_GUIDANCE
    if "contained_execute_python" in effective_tools:
        sections["contained_exec_guidance"] = CONTAINED_EXEC_GUIDANCE

    return sections
