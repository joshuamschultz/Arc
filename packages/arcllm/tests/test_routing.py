"""RoutingModule — the four-tier selection ladder and the continuity lock.

Selection order under test: pin > tool continuity > phrase > default. The
continuity tier is the load-bearing one: an agent turn is one user message
followed by many tool round-trips, and every one of those round-trips must
return to the model that asked for it.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from arcllm.embeddings import EmbeddingResponse
from arcllm.exceptions import ArcLLMConfigError, ArcLLMEmbeddingUnavailableError
from arcllm.modules.routing import Route, RoutingModule, parse_routes
from arcllm.types import (
    Delta,
    LLMProvider,
    LLMResponse,
    Message,
    TextBlock,
    ToolCall,
    ToolCallDelta,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

_USAGE = Usage(input_tokens=10, output_tokens=5, total_tokens=15)


def _response(model: str = "m", tool_calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        content="ok",
        tool_calls=tool_calls or [],
        usage=_USAGE,
        model=model,
        stop_reason="tool_use" if tool_calls else "end_turn",
    )


def _adapter(name: str, model: str, response: LLMResponse | None = None) -> MagicMock:
    adapter = MagicMock(spec=LLMProvider)
    adapter.name = name
    adapter.model_name = model
    adapter.validate_config.return_value = True
    adapter.invoke = AsyncMock(return_value=response or _response(model))
    adapter.close = AsyncMock()
    return adapter


class _StubEmbedder:
    """Exact-match embedder: known text maps to its own axis, all else to noise.

    Keeps the ladder under test rather than a similarity model — a phrase
    matches itself at 1.0 and matches nothing else at 0.0.
    """

    def __init__(self, vocabulary: list[str]) -> None:
        self._axis = {text: i for i, text in enumerate(vocabulary)}
        self._dims = len(vocabulary) + 1

    @property
    def model_name(self) -> str:
        return "stub"

    async def embed(self, texts: list[str]) -> EmbeddingResponse:
        vectors = []
        for text in texts:
            vector = [0.0] * self._dims
            vector[self._axis.get(text, self._dims - 1)] = 1.0
            vectors.append(vector)
        return EmbeddingResponse(vectors=vectors, dims=self._dims, model="stub", usage=_USAGE)


def _build(
    adapters: dict[str, MagicMock],
    *,
    phrases: dict[str, tuple[str, ...]] | None = None,
    **config: object,
) -> RoutingModule:
    phrases = phrases or {}
    routes = [
        Route(name=n, provider=a.name, model=a.model_name, phrases=phrases.get(n, ()))
        for n, a in adapters.items()
    ]
    settings: dict[str, object] = {"default_route": next(iter(adapters))}
    settings.update(config)
    return RoutingModule(settings, routes, lambda route: adapters[route.name])


def _pair() -> dict[str, MagicMock]:
    return {"default": _adapter("anthropic", "claude"), "local": _adapter("litellm", "qwen")}


@pytest.fixture
def hello():
    return [Message(role="user", content="hello")]


class TestSingleRoute:
    """One declared model must cost exactly what it cost before."""

    async def test_passes_through_to_the_only_adapter(self, hello):
        only = {"default": _adapter("anthropic", "claude")}
        router = _build(only)

        await router.invoke(hello)

        only["default"].invoke.assert_awaited_once()

    async def test_pin_is_stripped_before_dispatch(self, hello):
        """The pin steers the router; it must never reach the provider body."""
        only = {"default": _adapter("anthropic", "claude")}
        router = _build(only)

        await router.invoke(hello, route="default")

        assert "route" not in only["default"].invoke.await_args.kwargs

    async def test_no_route_metadata_stamped(self, hello):
        """Nothing chose anything, so nothing is claimed on the response."""
        only = {"default": _adapter("anthropic", "claude")}
        response = await _build(only).invoke(hello)
        assert response.metadata is None

    async def test_ungranted_pin_still_blocks_with_one_route(self, hello):
        """The fast path must not turn a rejected pin into a silent success."""
        only = {"default": _adapter("anthropic", "claude")}
        router = _build(only, enforcement="block")

        with pytest.raises(ArcLLMConfigError, match="Unknown route"):
            await router.invoke(hello, route="local")

        only["default"].invoke.assert_not_awaited()


class TestPinTier:
    async def test_pin_wins(self, hello):
        adapters = _pair()
        await _build(adapters).invoke(hello, route="local")
        adapters["local"].invoke.assert_awaited_once()

    async def test_pin_stripped_from_provider_kwargs(self, hello):
        adapters = _pair()
        await _build(adapters).invoke(hello, route="local")
        assert "route" not in adapters["local"].invoke.await_args.kwargs

    async def test_unknown_pin_warns_and_defaults(self, hello):
        adapters = _pair()
        router = _build(adapters, enforcement="warn")

        response = await router.invoke(hello, route="nope")

        adapters["default"].invoke.assert_awaited_once()
        assert response.metadata["arcllm_route_reason"] == "default"

    async def test_unknown_pin_blocks(self, hello):
        with pytest.raises(ArcLLMConfigError, match="Unknown route"):
            await _build(_pair(), enforcement="block").invoke(hello, route="nope")


class TestContinuityLock:
    """The tier that makes an agentic loop stay on one model."""

    async def test_whole_loop_stays_on_the_route_that_started_it(self):
        """One user turn, three calls, one model — the point of the design."""
        adapters = _pair()
        adapters["local"].invoke = AsyncMock(
            side_effect=[
                _response("qwen", [ToolCall(id="t1", name="read", arguments={})]),
                _response("qwen", [ToolCall(id="t2", name="read", arguments={})]),
                _response("qwen"),
            ]
        )
        router = _build(adapters)

        messages: list[Message] = [Message(role="user", content="go")]
        await router.invoke(messages, route="local")

        for call_id in ("t1", "t2"):
            messages.append(
                Message(
                    role="assistant", content=[ToolUseBlock(id=call_id, name="read", arguments={})]
                )
            )
            messages.append(
                Message(
                    role="user", content=[ToolResultBlock(tool_use_id=call_id, content="data")]
                )
            )
            await router.invoke(messages)

        assert adapters["local"].invoke.await_count == 3
        adapters["default"].invoke.assert_not_awaited()

    async def test_lock_beats_phrase_match(self, monkeypatch):
        """A tool result must not be re-read as a fresh request to reroute on."""
        adapters = _pair()
        adapters["default"].invoke = AsyncMock(
            return_value=_response("claude", [ToolCall(id="t1", name="read", arguments={})])
        )
        router = _build(adapters, phrases={"local": ("run this locally",)}, threshold=0.9)
        monkeypatch.setattr(
            "arcllm.embeddings.resolve_embedder",
            lambda *a, **k: _StubEmbedder(["run this locally"]),
        )

        messages: list[Message] = [Message(role="user", content="run this locally")]
        await router.invoke(messages, route="default")

        messages.append(
            Message(role="assistant", content=[ToolUseBlock(id="t1", name="read", arguments={})])
        )
        messages.append(
            Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="data")])
        )
        response = await router.invoke(messages)

        assert response.metadata["arcllm_route_reason"] == "tool_continuity"
        adapters["local"].invoke.assert_not_awaited()

    async def test_new_user_turn_after_a_closed_loop_reroutes(self, monkeypatch):
        """Stickiness ends at the turn boundary, not at the session boundary."""
        adapters = _pair()
        adapters["default"].invoke = AsyncMock(
            side_effect=[
                _response("claude", [ToolCall(id="t1", name="read", arguments={})]),
                _response("claude"),
            ]
        )
        router = _build(adapters, phrases={"local": ("run this locally",)}, threshold=0.9)
        monkeypatch.setattr(
            "arcllm.embeddings.resolve_embedder",
            lambda *a, **k: _StubEmbedder(["run this locally"]),
        )

        messages: list[Message] = [Message(role="user", content="first")]
        await router.invoke(messages, route="default")
        messages.append(
            Message(role="assistant", content=[ToolUseBlock(id="t1", name="read", arguments={})])
        )
        messages.append(
            Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="data")])
        )
        await router.invoke(messages)
        messages.append(Message(role="assistant", content="done"))
        messages.append(Message(role="user", content="run this locally"))

        response = await router.invoke(messages)

        assert response.metadata["arcllm_route"] == "local"

    async def test_streamed_tool_ids_are_learned(self):
        """Streaming must feed the lock too, or the loop breaks on real wires."""
        adapters = _pair()

        async def _stream(*_a, **_k):
            yield Delta(text="thinking")
            yield Delta(tool_call=ToolCallDelta(index=0, id="s1", name="read"))

        adapters["local"].invoke_stream = _stream
        router = _build(adapters)

        async for _ in router.invoke_stream([Message(role="user", content="go")], route="local"):
            pass

        assert router._tool_routes["s1"] == "local"


class TestPhraseTier:
    async def test_matching_phrase_selects_its_route(self, monkeypatch, hello):
        adapters = _pair()
        router = _build(adapters, phrases={"local": ("run this locally",)}, threshold=0.9)
        monkeypatch.setattr(
            "arcllm.embeddings.resolve_embedder",
            lambda *a, **k: _StubEmbedder(["run this locally"]),
        )

        response = await router.invoke([Message(role="user", content="run this locally")])

        assert response.metadata["arcllm_route"] == "local"
        assert response.metadata["arcllm_route_reason"] == "phrase"

    async def test_below_threshold_falls_to_default(self, monkeypatch):
        adapters = _pair()
        router = _build(adapters, phrases={"local": ("run this locally",)}, threshold=0.9)
        monkeypatch.setattr(
            "arcllm.embeddings.resolve_embedder",
            lambda *a, **k: _StubEmbedder(["run this locally"]),
        )

        response = await router.invoke([Message(role="user", content="what is the weather")])

        assert response.metadata["arcllm_route"] == "default"

    async def test_tool_result_is_not_treated_as_user_text(self, monkeypatch):
        """Tool results ride the user role; only prose may steer a route."""
        adapters = _pair()
        router = _build(adapters, phrases={"local": ("run this locally",)}, threshold=0.9)
        monkeypatch.setattr(
            "arcllm.embeddings.resolve_embedder",
            lambda *a, **k: _StubEmbedder(["run this locally"]),
        )

        response = await router.invoke(
            [
                Message(role="user", content="what is the weather"),
                Message(
                    role="user",
                    content=[ToolResultBlock(tool_use_id="unknown", content="run this locally")],
                ),
            ]
        )

        assert response.metadata["arcllm_route"] == "default"

    async def test_block_content_user_text_is_read(self, monkeypatch):
        adapters = _pair()
        router = _build(adapters, phrases={"local": ("run this locally",)}, threshold=0.9)
        monkeypatch.setattr(
            "arcllm.embeddings.resolve_embedder",
            lambda *a, **k: _StubEmbedder(["run this locally"]),
        )

        response = await router.invoke(
            [Message(role="user", content=[TextBlock(text="run this locally")])]
        )

        assert response.metadata["arcllm_route"] == "local"

    async def test_embedded_once_across_a_turn(self, monkeypatch):
        """The phrase index is built once, not per call."""
        adapters = _pair()
        router = _build(adapters, phrases={"local": ("run this locally",)}, threshold=0.9)
        builds = 0
        stub = _StubEmbedder(["run this locally"])

        def _resolve(*_a, **_k):
            nonlocal builds
            builds += 1
            return stub

        monkeypatch.setattr("arcllm.embeddings.resolve_embedder", _resolve)

        for _ in range(3):
            await router.invoke([Message(role="user", content="what is the weather")])

        # One resolve to build the index, then one per query embed — never a
        # rebuild of the phrase table.
        assert router._index.built
        assert builds == 4


class TestEmbedderFailure:
    async def test_missing_embedder_raises_by_default(self, monkeypatch, hello):
        """Configured phrases plus no embedder is a broken deployment, loudly."""
        adapters = _pair()
        router = _build(adapters, phrases={"local": ("run this locally",)})

        def _unavailable(*_a, **_k):
            raise ArcLLMEmbeddingUnavailableError("stub", "extra not installed")

        monkeypatch.setattr("arcllm.embeddings.resolve_embedder", _unavailable)

        with pytest.raises(ArcLLMConfigError, match="embedder"):
            await router.invoke(hello)

    async def test_embedder_failure_stays_closed_without_retrying(self, monkeypatch, hello):
        """Fail-closed must keep failing, and must not reload the model each call."""
        adapters = _pair()
        router = _build(adapters, phrases={"local": ("run this locally",)})
        attempts = 0

        def _unavailable(*_a, **_k):
            nonlocal attempts
            attempts += 1
            raise ArcLLMEmbeddingUnavailableError("stub", "extra not installed")

        monkeypatch.setattr("arcllm.embeddings.resolve_embedder", _unavailable)

        for _ in range(3):
            with pytest.raises(ArcLLMConfigError, match="embedder"):
                await router.invoke(hello)

        assert attempts == 1

    async def test_opt_in_fallback_logs_and_defaults(self, monkeypatch, hello, caplog):
        adapters = _pair()
        router = _build(
            adapters,
            phrases={"local": ("run this locally",)},
            on_embedder_error="default_route",
        )

        def _unavailable(*_a, **_k):
            raise ArcLLMEmbeddingUnavailableError("stub", "extra not installed")

        monkeypatch.setattr("arcllm.embeddings.resolve_embedder", _unavailable)

        with caplog.at_level("ERROR"):
            response = await router.invoke(hello)

        assert response.metadata["arcllm_route"] == "default"
        assert "Semantic routing DISABLED" in caplog.text

    async def test_routes_without_phrases_never_touch_the_embedder(self, monkeypatch, hello):
        """No phrases means no index, so a missing embedder is not an error."""
        adapters = _pair()
        router = _build(adapters)

        def _boom(*_a, **_k):
            raise AssertionError("embedder must not be resolved")

        monkeypatch.setattr("arcllm.embeddings.resolve_embedder", _boom)

        response = await router.invoke(hello)
        assert response.metadata["arcllm_route"] == "default"


class TestLifecycle:
    async def test_default_eager_alternates_lazy(self, hello):
        """A bad default must fail at construction; an unused alternate costs nothing."""
        adapters = _pair()
        built: list[str] = []
        routes = [Route(name=n, provider=a.name, model=a.model_name) for n, a in adapters.items()]

        def _factory(route):
            built.append(route.name)
            return adapters[route.name]

        router = RoutingModule({"default_route": "default"}, routes, _factory)
        assert built == ["default"]

        await router.invoke(hello)
        assert built == ["default"]

        await router.invoke(hello, route="local")
        assert built == ["default", "local"]

    def test_broken_default_route_raises_at_construction(self):
        """The failure an operator can act on is the one at startup."""
        routes = [Route(name="default", provider="p"), Route(name="alt", provider="q")]

        def _factory(route):
            raise ArcLLMConfigError(f"no adapter for {route.provider}")

        with pytest.raises(ArcLLMConfigError, match="no adapter for p"):
            RoutingModule({"default_route": "default"}, routes, _factory)

    async def test_close_tolerates_one_failing_adapter(self, hello):
        adapters = _pair()
        adapters["default"].close = AsyncMock(side_effect=RuntimeError("boom"))
        router = _build(adapters)
        router.adapter_for("default")
        router.adapter_for("local")

        with pytest.raises(ExceptionGroup):
            await router.close()

        adapters["local"].close.assert_awaited_once()

    def test_validate_config_covers_unbuilt_routes(self):
        adapters = _pair()
        adapters["local"].validate_config.return_value = False
        router = _build(adapters)

        assert router.validate_config() is False

    def test_properties_report_the_default_route(self):
        router = _build(_pair())
        assert router.name == "anthropic"
        assert router.model_name == "claude"
        assert router.routes == ("default", "local")


class TestConstruction:
    def test_empty_routes_rejected(self):
        with pytest.raises(ArcLLMConfigError, match="at least one route"):
            RoutingModule({}, [], lambda r: MagicMock())

    def test_duplicate_route_names_rejected(self):
        routes = [Route(name="a", provider="p"), Route(name="a", provider="q")]
        with pytest.raises(ArcLLMConfigError, match="Duplicate route names"):
            RoutingModule({}, routes, lambda r: MagicMock())

    def test_default_route_must_exist(self):
        routes = [Route(name="a", provider="p")]
        with pytest.raises(ArcLLMConfigError, match="not a declared route"):
            RoutingModule({"default_route": "missing"}, routes, lambda r: MagicMock())

    def test_unknown_config_key_rejected(self):
        routes = [Route(name="a", provider="p")]
        with pytest.raises(ArcLLMConfigError, match="Unknown RoutingModule config keys"):
            RoutingModule({"treshold": 0.5}, routes, lambda r: MagicMock())

    def test_out_of_range_threshold_rejected(self):
        routes = [Route(name="a", provider="p")]
        with pytest.raises(ArcLLMConfigError, match="threshold must be between"):
            RoutingModule({"threshold": 1.5}, routes, lambda r: MagicMock())

    def test_invalid_embedder_error_policy_rejected(self):
        routes = [Route(name="a", provider="p")]
        with pytest.raises(ArcLLMConfigError, match="on_embedder_error"):
            RoutingModule({"on_embedder_error": "shrug"}, routes, lambda r: MagicMock())


class TestParseRoutes:
    def test_callers_default_becomes_a_route(self):
        routes = parse_routes(None, default_provider="anthropic", default_model="claude")
        assert [r.name for r in routes] == ["default"]
        assert routes[0].provider == "anthropic"
        assert routes[0].model == "claude"

    def test_declared_routes_join_the_default(self):
        routes = parse_routes(
            {"local": {"model": "litellm/qwen", "phrases": ["run this locally"]}},
            default_provider="anthropic",
            default_model="claude",
        )
        assert [r.name for r in routes] == ["default", "local"]
        local = routes[1]
        assert (local.provider, local.model) == ("litellm", "qwen")
        assert local.phrases == ("run this locally",)

    def test_declared_default_is_not_overwritten(self):
        routes = parse_routes(
            {"default": {"model": "ollama/llama"}},
            default_provider="anthropic",
            default_model="claude",
        )
        assert len(routes) == 1
        assert routes[0].provider == "ollama"

    def test_bare_provider_route_keeps_provider_default_model(self):
        routes = parse_routes(
            {"local": {"model": "ollama"}},
            default_provider="anthropic",
            default_model="claude",
        )
        assert routes[1].model is None

    def test_route_without_model_rejected(self):
        with pytest.raises(ArcLLMConfigError, match="missing 'model'"):
            parse_routes(
                {"local": {"phrases": ["x"]}},
                default_provider="anthropic",
                default_model="claude",
            )

    def test_non_string_phrases_rejected(self):
        with pytest.raises(ArcLLMConfigError, match="must be a list of strings"):
            parse_routes(
                {"local": {"model": "ollama/x", "phrases": [1, 2]}},
                default_provider="anthropic",
                default_model="claude",
            )
