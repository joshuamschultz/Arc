"""Strategy prompt provider — model-facing guidance for ArcRun capabilities.

ArcRun owns the strategies and their documentation. Consuming agents
(e.g. ArcAgent) call get_strategy_prompts() to obtain prompt fragments
they inject into the system prompt. This keeps separation clean:

- ArcRun knows WHAT strategies do and WHEN to use them
- The consuming agent owns prompt ASSEMBLY and decides what goes in

See ADR on strategy prompt injection for architectural rationale.
"""

from __future__ import annotations

from arcrun.strategies import STRATEGIES, _load_strategies

# ---------------------------------------------------------------------------
# Builtin tool guidance constants
# ---------------------------------------------------------------------------

SPAWN_GUIDANCE = """\
## Task Delegation (spawn_task)
Before executing multi-step work sequentially, evaluate whether subtasks
should be delegated to child agents. Use spawn_task when:
- Subtasks are independent and can run in parallel
- A subtask requires specialized focus (research, verification, review)
- A subtask would consume significant context that the main thread does
  not need to retain
- The task naturally decomposes into separable pieces

Do NOT use spawn_task when:
- The subtask depends on results from the current conversation
- The overhead of context transfer outweighs the benefit
- A single tool call would accomplish the same thing

Always include ALL necessary context in the task description — child
agents have no memory of this conversation. The child inherits your
tools and system prompt but starts with a fresh message history.

<example>
User asks: "Research the top 3 competing products and summarize their pricing"

Good delegation:
  spawn_task(task="Research and summarize pricing for [Product A]. Include: ...")
  spawn_task(task="Research and summarize pricing for [Product B]. Include: ...")
  spawn_task(task="Research and summarize pricing for [Product C]. Include: ...")
  → Three independent research tasks run concurrently

Bad delegation:
  spawn_task(task="Do step 1 of the analysis")
  → Too vague, lacks context, child cannot succeed
</example>"""

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
    spawn_enabled: bool = True,
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
        spawn_enabled: Whether spawn_task will be injected (True when
            depth < max_depth, which is the default).
        tool_names: Names of tools that will be available. Used to
            detect code execution tools and include their guidance.

    Returns:
        Dict mapping section names to prompt text. Keys are stable
        identifiers (e.g. 'loop_behavior', 'spawn_guidance') that
        the consuming agent can use for ordering and caching.
    """
    if not STRATEGIES:
        _load_strategies()

    sections: dict[str, str] = {}
    effective_tools = tool_names or []

    # --- Core loop behavior (from active strategy) ---
    effective_strategies = allowed_strategies or ["react"]
    # Include guidance for all allowed strategies
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

    # --- Spawn guidance ---
    if spawn_enabled:
        sections["spawn_guidance"] = SPAWN_GUIDANCE

    # --- Code execution guidance ---
    if "execute_python" in effective_tools:
        sections["code_exec_guidance"] = CODE_EXEC_GUIDANCE
    if "contained_execute_python" in effective_tools:
        sections["contained_exec_guidance"] = CONTAINED_EXEC_GUIDANCE

    return sections
