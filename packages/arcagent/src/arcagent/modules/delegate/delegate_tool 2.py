"""Agent-facing `delegate` tool — thin wrapper over arcrun.spawn().

Implements SDD §3.5 T3.6:
- Calls arcrun.spawn() with a properly constrained child tool list
- Strips DELEGATE_BLOCKED_TOOLS from child tool list
- Intersects requested tools with parent allowlist (no privilege escalation)
- Enforces tier-driven depth caps
- Returns structured SpawnResult serialized as JSON string to LLM

Security (ASI-02, ASI-03, ASI-08, LLM06):
- DELEGATE_BLOCKED_TOOLS removed unconditionally — model cannot override
- Child tool list is intersection of parent's actual tools + requested names
- Depth enforced at both arcrun layer (max_depth) and this layer (config)
- Error messages sanitized before reaching LLM output (LLM02)

Depth tracking (M3 gap-close):
- ctx.parent_state carries the live RunState from executor.py (set since M3 fix)
- parent_depth = ctx.parent_state.depth when available, else 0 (backward compat)
- child RunState depth = parent_depth + 1
- Depth cap check: if parent_depth + 1 > cfg.max_depth → reject immediately
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from arcrun.builtins.spawn import (
    ChildIdentity,
    SpawnResult,
    derive_child_identity,
    spawn,
)
from arcrun.types import Tool, ToolContext

from arcagent.modules.delegate.config import DELEGATE_BLOCKED_TOOLS, DelegateConfig

_logger = logging.getLogger("arcagent.modules.delegate")

_MAX_ERR_LEN = 200

# Vocabulary map: exception type → stable token sent to LLM (LLM02 — prevents
# leaking internal class names as prompt-injectable surface area).
_EXC_VOCAB: dict[type[BaseException], str] = {
    asyncio.TimeoutError: "timeout",
    asyncio.CancelledError: "interrupted",
    ValueError: "bad_args",
    TypeError: "bad_args",
}
_DEFAULT_EXC_VOCAB = "internal_error"


def _exc_to_vocab(exc: BaseException) -> str:
    """Map an exception to its LLM-facing vocabulary token.

    Uses the most-specific matching type in _EXC_VOCAB; falls back to
    ``_DEFAULT_EXC_VOCAB`` so internal class names never reach the LLM.
    The real exception type is always logged server-side.
    """
    for exc_type, token in _EXC_VOCAB.items():
        if isinstance(exc, exc_type):
            return token
    return _DEFAULT_EXC_VOCAB


def _build_child_tool_list(
    parent_tools: list[Tool],
    requested_names: list[str] | None,
) -> tuple[list[Tool], list[str]]:
    """Build child tool list by intersecting parent allowlist and stripping blocked tools.

    Args:
        parent_tools: The full set of tools available to the parent agent.
        requested_names: Optional list of tool names requested by the LLM.
            If None, child inherits all parent tools minus blocked ones.

    Returns:
        Tuple of (allowed_tools, stripped_names) where stripped_names lists
        the tools that were removed due to DELEGATE_BLOCKED_TOOLS.
    """
    # Start from parent's tool list (allowlist intersection prevents escalation)
    if requested_names is not None:
        candidate_tools = [t for t in parent_tools if t.name in set(requested_names)]
    else:
        candidate_tools = list(parent_tools)

    # Unconditionally strip blocked tools — model cannot override this
    allowed: list[Tool] = []
    stripped: list[str] = []
    for tool in candidate_tools:
        if tool.name in DELEGATE_BLOCKED_TOOLS:
            stripped.append(tool.name)
        else:
            allowed.append(tool)

    return allowed, stripped


def make_delegate_tool(
    *,
    parent_tools: list[Tool],
    config: DelegateConfig | None = None,
    parent_sk_bytes: bytes | None = None,
) -> Tool:
    """Create the agent-facing `delegate` tool.

    Factory captures parent tool list and config via closure. The model
    sees a schema; this layer enforces all security invariants before
    delegating to arcrun.spawn().

    Args:
        parent_tools: Parent agent's full tool list (before blocking).
        config: DelegateConfig; defaults to personal-tier config if None.
        parent_sk_bytes: Parent agent's Ed25519 signing key bytes for HKDF.
            If None, zero bytes are used (identity derivation still works
            but produces the same key for all spawns — acceptable for dev).

    Returns:
        A Tool named "delegate" that the agent can call.
    """
    cfg = config or DelegateConfig()
    sk_bytes = parent_sk_bytes or b"\x00" * 32

    async def _execute(params: dict[str, Any], ctx: ToolContext) -> str:
        task = params["task"]
        context = params.get("context")
        requested_names: list[str] | None = params.get("tools")
        max_turns = int(params.get("max_turns", cfg.default_max_turns))
        token_budget: int | None = params.get("token_budget")
        timeout_s = int(params.get("timeout_s", cfg.default_timeout_s))
        system_prompt_extra: str = params.get("system_prompt", "")

        # Resolve parent depth from the live RunState plumbed via ToolContext.
        # ctx.parent_state is set by executor.py (M3 gap-close); fall back to 0
        # for backward-compat call sites (unit tests, legacy callers) where
        # parent_state is None.
        parent_depth: int = ctx.parent_state.depth if ctx.parent_state is not None else 0

        # Depth cap enforcement: child would be at parent_depth + 1.
        # Reject before spawning so the LLM gets a clear error rather than a
        # runtime failure deep inside spawn().
        child_depth = parent_depth + 1
        if child_depth > cfg.max_depth:
            _logger.warning(
                "delegate: depth cap exceeded (parent_depth=%d, max_depth=%d)",
                parent_depth,
                cfg.max_depth,
            )
            return json.dumps(
                {
                    "status": "error",
                    "error": "max_depth",
                    "detail": (
                        f"Delegation rejected: child depth {child_depth} "
                        f"exceeds max_depth {cfg.max_depth}"
                    ),
                }
            )

        # Build constrained tool list (intersection + strip blocked)
        child_tools, stripped = _build_child_tool_list(parent_tools, requested_names)

        if stripped:
            _logger.info(
                "delegate: stripped blocked tools from child tool list: %s", stripped
            )

        if not child_tools:
            return json.dumps(
                {
                    "status": "error",
                    "error": "no_tools_available",
                    "detail": "No tools available for child after allowlist intersection",
                }
            )

        # Derive per-child identity via HKDF
        spawn_id = str(uuid.uuid4())
        child_identity: ChildIdentity = derive_child_identity(
            parent_sk_bytes=sk_bytes,
            spawn_id=spawn_id,
            wallclock_timeout_s=timeout_s,
        )

        # Build system prompt for child — delegation context injected
        base_prompt = (
            "You are a focused sub-agent. Complete the delegated task precisely. "
            "Do not attempt to spawn additional agents or access memory outside "
            "the provided context."
        )
        if system_prompt_extra:
            child_system_prompt = f"{base_prompt}\n\n{system_prompt_extra}"
        else:
            child_system_prompt = base_prompt

        # Build parent RunState for spawn().
        # Use the live parent RunState from ToolContext when available so spawn()
        # inherits the correct depth, max_depth, token budget, etc.
        # When parent_state is None (legacy / test paths) build a minimal adapter.
        from arcrun.events import EventBus
        from arcrun.registry import ToolRegistry
        from arcrun.state import RunState

        if ctx.parent_state is not None:
            # Use the actual parent RunState — depth is already correct.
            parent_run_state = ctx.parent_state
        else:
            # Backward-compat: construct minimal stub at depth 0.
            bus = ctx.event_bus
            if bus is None:
                bus = EventBus(run_id=ctx.run_id)
            parent_run_state = RunState(
                messages=[],
                registry=ToolRegistry(tools=parent_tools, event_bus=bus),
                event_bus=bus,
                run_id=ctx.run_id,
                depth=0,
                max_depth=cfg.max_depth,
            )

        try:
            result: SpawnResult = await spawn(
                parent_state=parent_run_state,
                task=task,
                context=context,
                tools=child_tools,
                system_prompt=child_system_prompt,
                identity=child_identity,
                max_turns=max_turns,
                token_budget=token_budget,
                wallclock_timeout_s=timeout_s,
            )
        except Exception as exc:
            # Log the real exception type server-side for ops visibility;
            # return a stable vocabulary token to the LLM to prevent exception
            # class names from becoming an injection-probe surface (LLM02).
            _logger.warning(
                "delegate tool: spawn failed: %s: %s",
                type(exc).__name__,
                str(exc)[:_MAX_ERR_LEN],
            )
            return json.dumps(
                {
                    "status": "error",
                    "error": _exc_to_vocab(exc),
                    "detail": "Delegation failed; child task could not be started",
                }
            )

        # Serialize SpawnResult as JSON string to LLM
        return json.dumps(
            {
                "child_run_id": result.child_run_id,
                "child_did": result.child_did,
                "status": result.status,
                "summary": result.summary,
                "tokens": {
                    "input": result.tokens.input,
                    "output": result.tokens.output,
                    "total": result.tokens.total,
                },
                "tool_trace": result.tool_trace,
                "duration_s": result.duration_s,
                "error": result.error,
                "stripped_tools": stripped,
            }
        )

    return Tool(
        name="delegate",
        description=(
            "Delegate a sub-task to a child agent. The child runs independently "
            "with a restricted tool set and returns its result. Use for task "
            "decomposition, parallel work, or focused sub-tasks. "
            "Note: 'delegate', 'memory', 'send_message', 'execute_code', and "
            "'clarify' tools are automatically excluded from the child's tool list."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task for the child agent to accomplish",
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Optional structured context to provide to the child "
                        "(non-instructional data only)"
                    ),
                },
                "system_prompt": {
                    "type": "string",
                    "description": (
                        "Optional specialization for the child's system prompt. "
                        "Cannot replace the base delegation prompt."
                    ),
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of tool names the child may use. "
                        "If omitted, inherits all parent tools minus blocked ones. "
                        "Blocked tools are stripped regardless."
                    ),
                },
                "max_turns": {
                    "type": "integer",
                    "description": "Max turns for child (default 25)",
                    "minimum": 1,
                    "maximum": 200,
                },
                "token_budget": {
                    "type": "integer",
                    "description": "Token budget drawn from parent's root pool",
                    "minimum": 1,
                },
                "timeout_s": {
                    "type": "integer",
                    "description": "Wall-clock timeout in seconds (default 300)",
                    "minimum": 10,
                    "maximum": 3600,
                },
            },
            "required": ["task"],
        },
        execute=_execute,
        timeout_seconds=cfg.default_timeout_s + 30,  # slight grace beyond child timeout
    )
