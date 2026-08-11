"""The continuity lock holds across a real arcrun ReAct loop.

Unit tests for the router build their own message lists, which proves the rule
and not the wiring. This drives the actual loop, so the messages under test are
the ones arcrun really produces — ``role="tool"`` results, assistant
``tool_use`` blocks, parallel dispatch. If arcrun ever changes that shape the
lock silently stops firing, and this is what fails.

Note what the loop cannot do: ``react_loop`` has no way to pass a ``route`` pin,
by design. Inside an agentic run only tiers 2-4 of the ladder are reachable, so
the first turn is chosen by phrases or the default, and every turn after it by
the lock.

arcrun must not import ``arcllm.registry`` (architecture rule), so the router is
constructed directly and handed in — exactly how arcagent hands one over.
"""

from typing import Any

import pytest
from arcllm.embeddings import EmbeddingResponse
from arcllm.modules.routing import Route, RoutingModule
from arcllm.types import LLMResponse, Message, ToolCall, Usage

from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies.react import react_loop
from arcrun.types import SandboxConfig, Tool

_USAGE = Usage(input_tokens=1, output_tokens=1, total_tokens=2)


class _Adapter:
    """Minimal LLMProvider the router can dispatch to."""

    def __init__(self, label: str, responses: list[LLMResponse]) -> None:
        self._label = label
        self._responses = list(responses)
        self.calls: list[list[Message]] = []

    @property
    def name(self) -> str:
        return self._label

    @property
    def model_name(self) -> str:
        return f"{self._label}-model"

    async def invoke(
        self, messages: list[Message], tools: Any = None, **kwargs: Any
    ) -> LLMResponse:
        self.calls.append(list(messages))
        if not self._responses:
            raise RuntimeError(f"{self._label} adapter exhausted responses")
        return self._responses.pop(0)

    def validate_config(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class _StubEmbedder:
    """Known text maps to its own axis; everything else to a shared other axis."""

    def __init__(self, vocabulary: list[str]) -> None:
        self._axis = {text: i for i, text in enumerate(vocabulary)}
        self._dims = len(vocabulary) + 1
        self.embed_calls = 0

    @property
    def model_name(self) -> str:
        return "stub"

    async def embed(self, texts: list[str]) -> EmbeddingResponse:
        self.embed_calls += 1
        vectors = []
        for text in texts:
            vector = [0.0] * self._dims
            vector[self._axis.get(text, self._dims - 1)] = 1.0
            vectors.append(vector)
        return EmbeddingResponse(vectors=vectors, dims=self._dims, model="stub", usage=_USAGE)


def _tool_call(call_id: str, value: str) -> ToolCall:
    return ToolCall(id=call_id, name="echo", arguments={"input": value})


def _tool_use(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(
        content=None, tool_calls=list(calls), usage=_USAGE, model="m", stop_reason="tool_use"
    )


def _done(text: str) -> LLMResponse:
    return LLMResponse(content=text, usage=_USAGE, model="m", stop_reason="end_turn")


async def _echo(params: dict, ctx: object) -> str:
    return f"echo: {params.get('input', '')}"


def _router(default: _Adapter, local: _Adapter, *, phrases: tuple[str, ...] = ()) -> RoutingModule:
    adapters = {"default": default, "local": local}
    routes = [
        Route(name="default", provider="a", model="m1"),
        Route(name="local", provider="b", model="m2", phrases=phrases),
    ]
    return RoutingModule(
        {"default_route": "default", "threshold": 0.9},
        routes,
        lambda route: adapters[route.name],
    )


def _state(bus: EventBus, task: str) -> RunState:
    tool = Tool(
        name="echo",
        description="Echo input",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
        execute=_echo,
    )
    return RunState(
        messages=[
            Message(role="system", content="You are helpful."),
            Message(role="user", content=task),
        ],
        registry=ToolRegistry(tools=[tool], event_bus=bus),
        event_bus=bus,
        run_id="routing-continuity",
    )


async def _run(router: RoutingModule, task: str) -> Any:
    bus = EventBus(run_id="routing-continuity")
    return await react_loop(
        model=router,
        state=_state(bus, task),
        sandbox=Sandbox(config=SandboxConfig(), event_bus=bus),
        max_turns=10,
    )


class TestLoopStaysOnOneRoute:
    @pytest.mark.asyncio
    async def test_phrase_picks_the_route_and_the_loop_keeps_it(self, monkeypatch):
        """One phrase match on turn one; every tool turn returns to that model.

        The route assertion alone cannot fail if the lock is deleted — the last
        user message never changes during a turn, so the phrase tier would
        re-derive the same winner and the loop would look identical. What only
        the lock produces is *not asking again*: one index build plus exactly
        one query embed for the whole three-call turn. Counting embeds is the
        assertion that actually holds the lock in place.
        """
        stub = _StubEmbedder(["run this locally"])
        monkeypatch.setattr("arcllm.embeddings.resolve_embedder", lambda *a, **k: stub)
        local = _Adapter(
            "local",
            [
                _tool_use(_tool_call("t1", "a")),
                _tool_use(_tool_call("t2", "b")),
                _done("All done."),
            ],
        )
        default = _Adapter("default", [_done("wrong model")])

        result = await _run(
            _router(default, local, phrases=("run this locally",)), "run this locally"
        )

        assert result.content == "All done."
        assert len(local.calls) == 3
        assert default.calls == []
        # 1 index build + 1 query. Three queries would mean the lock never fired.
        assert stub.embed_calls == 2

    @pytest.mark.asyncio
    async def test_parallel_tool_results_still_lock(self, monkeypatch):
        """A turn answering several tool calls at once locks on any of them."""
        monkeypatch.setattr(
            "arcllm.embeddings.resolve_embedder",
            lambda *a, **k: _StubEmbedder(["run this locally"]),
        )
        local = _Adapter(
            "local",
            [
                _tool_use(_tool_call("p1", "a"), _tool_call("p2", "b")),
                _done("Both done."),
            ],
        )
        default = _Adapter("default", [_done("wrong model")])

        result = await _run(
            _router(default, local, phrases=("run this locally",)), "run this locally"
        )

        assert result.content == "Both done."
        assert len(local.calls) == 2
        assert default.calls == []

    @pytest.mark.asyncio
    async def test_unmatched_task_keeps_the_whole_loop_on_the_default(self, monkeypatch):
        monkeypatch.setattr(
            "arcllm.embeddings.resolve_embedder",
            lambda *a, **k: _StubEmbedder(["run this locally"]),
        )
        default = _Adapter("default", [_tool_use(_tool_call("d1", "a")), _done("Done.")])
        local = _Adapter("local", [_done("wrong model")])

        result = await _run(
            _router(default, local, phrases=("run this locally",)), "what is the weather"
        )

        assert result.content == "Done."
        assert len(default.calls) == 2
        assert local.calls == []
