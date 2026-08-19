"""OneShot strategy: one bounded model call, no tools, no iteration.

Some decisions are worth a model and not worth a run — a yes/no gate, a label,
a tiebreak between candidates already narrowed by cheaper means. Those callers
live above arcrun and used to satisfy the need by holding a provider handle and
invoking it themselves, which put LLM-call logic in a layer that must not own
it (ADR-032).

Cheap inference is a real need, so it gets a real strategy rather than a side
door. The bound is the point: one turn, an empty tool set, an output ceiling,
and a deadline. A caller that wants more than one call wants ``react``.
"""

from __future__ import annotations

from typing import Any

from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies import Strategy
from arcrun.strategies.react import accumulate_usage, build_result
from arcrun.types import LoopResult


class OneShotStrategy(Strategy):
    """One model call against the state's messages, then done."""

    @property
    def name(self) -> str:
        return "oneshot"

    @property
    def auto_selectable(self) -> bool:
        """Never offered to the selector: it answers once and cannot use a tool.

        Auto-selection asks which shape fits an agentic task. This shape fits no
        agentic task, so a model picking it would silently strip the run's tools
        and return after a single turn.
        """
        return False

    async def __call__(
        self,
        model: Any,
        state: RunState,
        sandbox: Sandbox,
        max_turns: int,
    ) -> LoopResult:
        state.max_turns = 1
        # No ceiling means no kwarg: a caller who declined to cap the output must
        # not have one invented for it by a default further down the stack.
        cap = {"max_tokens": state.max_tokens} if state.max_tokens is not None else {}
        response = await model.invoke(state.messages, **cap)
        accumulate_usage(state, response)
        state.turn_count = 1
        state.event_bus.emit("turn.end", {"turn_number": 1})
        return build_result(state, (response.content or "").strip() or None)
