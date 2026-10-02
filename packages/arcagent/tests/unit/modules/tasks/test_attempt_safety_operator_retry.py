"""An operator-retried node row repeats a side effect only when the operator accepted it.

The control plane stamps ``operator_retry`` on the row a node retry creates. That
row's FIRST claim is attempt 1, so counting attempts alone would let the repeat
through silently; the stamp is what makes the executor treat it as a re-run.
"""

from __future__ import annotations

from typing import Any

from .test_attempt_keys import _claim, _todo_node, state  # noqa: F401
from .test_attempt_safety import _run, _Tool


async def test_operator_retried_row_refuses_non_idempotent_tool_without_accept(
    state: Any,  # noqa: F811
) -> None:
    tool = _Tool(idempotent=False)
    state.tool_registry = tool
    await _todo_node(state, operator_retry=True)
    row = await _claim(state)
    assert row.attempts == 1

    await _run(state, row)

    assert tool.calls == 0
    after = await state.store.get(row.id)
    assert after is not None and after.status == "failed"
    assert "operator_retry_ok" in (after.last_error or "")


async def test_operator_retried_row_runs_non_idempotent_tool_when_accepted(
    state: Any,  # noqa: F811
) -> None:
    tool = _Tool(idempotent=False)
    state.tool_registry = tool
    await _todo_node(state, operator_retry=True, operator_retry_ok=True)
    row = await _claim(state)

    await _run(state, row)

    assert tool.calls == 1


async def test_operator_retried_row_runs_an_idempotent_tool(state: Any) -> None:  # noqa: F811
    tool = _Tool(idempotent=True)
    state.tool_registry = tool
    await _todo_node(state, operator_retry=True)
    row = await _claim(state)

    await _run(state, row)

    assert tool.calls == 1
