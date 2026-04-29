"""SPEC-021 capability surface for the delegate module.

Exposes the ``delegate`` tool via the ``@tool`` decorator so the
:class:`CapabilityLoader` can discover and register it without the old
Module-protocol pattern.

The core delegation logic lives in :mod:`arcagent.modules.delegate.delegate_tool`
and is shared with :func:`make_delegate_tool` so both paths stay in sync
without duplication.

Parent-state plumbing (follow-up note)
---------------------------------------
The arcrun-style ``_execute(params, ctx)`` signature gives the tool access
to ``ctx.parent_state`` (the live :class:`RunState`). The ``@tool``
decorator pattern wraps the function as ``(**kwargs) -> str`` at the
registry layer; ``ctx`` is not passed through that wrapper yet.

Until the registry layer threads ``ctx`` through (SPEC-021 follow-up),
the implementation raises :class:`NotImplementedError` with a clear
message. The agent startup wiring should call
:func:`arcagent.modules.delegate._runtime.configure` so that
``parent_tools``, ``parent_sk_bytes``, and ``config`` are available;
``parent_state`` will need to be injected by the executor before the tool
is dispatched (same requirement as ``make_delegate_tool``).
"""

from __future__ import annotations

from arcagent.modules.delegate import _runtime
from arcagent.tools._decorator import tool


@tool(
    name="delegate",
    description=(
        "Delegate a sub-task to a child agent. The child runs independently "
        "with a restricted tool set and returns its result. Use for task "
        "decomposition, parallel work, or focused sub-tasks. "
        "Note: 'delegate', 'memory', 'send_message', 'execute_code', and "
        "'clarify' tools are automatically excluded from the child's tool list."
    ),
    classification="state_modifying",
    capability_tags=["delegation", "spawn", "multi_agent"],
    when_to_use=(
        "When a task is large enough to benefit from a focused sub-agent, "
        "or when parallel independent work streams are needed. "
        "Do NOT use for simple tasks you can complete in the current turn."
    ),
    version="1.0.0",
)
async def delegate(
    task: str,
    context: str = "",
    system_prompt: str = "",
    tools: list = [],  # noqa: B006  # list literal required for schema inference
    max_turns: int = 25,
    token_budget: int = 0,
    timeout_s: int = 300,
) -> str:
    """Spawn a child agent to accomplish *task*.

    Args:
        task: The task for the child agent to accomplish.
        context: Optional structured context to provide to the child
            (non-instructional data only).
        system_prompt: Optional specialization for the child's system prompt.
            Cannot replace the base delegation prompt.
        tools: Optional list of tool names the child may use. If empty,
            inherits all parent tools minus blocked ones.
        max_turns: Max turns for child (1-200, default 25).
        token_budget: Token budget drawn from parent's root pool. 0 means
            no explicit budget (inherits from parent).
        timeout_s: Wall-clock timeout in seconds (10-3600, default 300).

    Returns:
        JSON string with child run result or error payload.

    Raises:
        NotImplementedError: Until the registry layer threads ToolContext
            through the ``@tool`` wrapper, this tool cannot obtain
            ``ctx.parent_state`` and therefore cannot call
            ``arcagent.orchestration.spawn``. The agent startup must wire
            ``parent_state`` through the executor before dispatching.
    """
    # _runtime.state() validates the module was configured at startup
    # (parent_tools, parent_sk_bytes, config are all available here).
    # What is NOT yet available via the @tool wrapper is ctx.parent_state,
    # which carries the live RunState (depth, token pool, event bus).
    #
    # The registry layer must inject parent_state before dispatching.
    # Tracked as SPEC-021 follow-up: thread ctx through @tool wrappers.
    _runtime.state()  # raises RuntimeError if unconfigured -- fail fast

    raise NotImplementedError(
        "delegate: parent_state plumbing pending. "
        "The @tool registry wrapper must inject ToolContext before dispatch. "
        "Use make_delegate_tool() directly until SPEC-021 ctx threading is complete."
    )
