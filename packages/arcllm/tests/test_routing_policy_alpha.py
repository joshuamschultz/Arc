import asyncio

import pytest

from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules.routing import Route, RoutingModule
from arcllm.types import Delta, LLMResponse, Message, Tool, Usage

_USAGE = Usage(input_tokens=1, output_tokens=1, total_tokens=2)


class Provider:
    def __init__(self, name: str, model: str) -> None:
        self.name, self.model_name, self.calls = name, model, 0
        self.stream_calls = 0

    async def invoke(self, messages, tools=None, **kwargs):
        self.calls += 1
        await asyncio.sleep(0)
        return LLMResponse(
            content="ok", usage=_USAGE, model=self.model_name, stop_reason="end_turn"
        )

    async def invoke_stream(self, messages, tools=None, **kwargs):
        self.stream_calls += 1
        await asyncio.sleep(0)
        yield Delta(text=self.model_name)

    def validate_config(self):
        return True

    async def close(self):
        return None


def build_router(**config):
    providers = {"default": Provider("p", "cheap"), "strong": Provider("p", "strong")}
    routes = [Route("default", "p", "cheap"), Route("strong", "p", "strong")]
    return RoutingModule(
        {"default_route": "default", **config}, routes, lambda r: providers[r.name]
    ), providers


@pytest.mark.asyncio
async def test_continuity_lock_is_session_scoped_and_beats_pin():
    router, providers = build_router(lock_capacity=8)
    from arcllm.types import ToolResultBlock, ToolUseBlock

    router._remember_tool_calls(["same-id"], "strong", "s1")
    router._remember_tool_calls(["same-id"], "default", "s2")
    messages = [
        Message(role="assistant", content=[ToolUseBlock(id="same-id", name="x", arguments={})]),
        Message(role="user", content=[ToolResultBlock(tool_use_id="same-id", content="ok")]),
    ]
    await router.invoke(messages, route="default", session_id="s1")
    assert providers["strong"].calls == 1
    assert providers["default"].calls == 0


@pytest.mark.asyncio
async def test_capability_gate_blocks_tool_incompatible_route():
    router, providers = build_router(enforcement="block")
    router._routes["default"] = Route("default", "p", "cheap", capabilities=())
    router._routes["strong"] = Route("strong", "p", "strong", capabilities=())
    with pytest.raises(ArcLLMConfigError, match="capabil"):
        await router.invoke(
            [Message(role="user", content="go")],
            tools=[Tool(name="x", description="x", parameters={})],
        )
    assert providers["default"].calls == 0


@pytest.mark.asyncio
async def test_concurrent_request_policies_do_not_leak():
    providers = {name: Provider("p", name) for name in ("secure", "public")}
    routes = [
        Route("secure", "p", "secure", classification_max="cui", residency="secure"),
        Route("public", "p", "public", classification_max="top_secret", residency="public"),
    ]
    router = RoutingModule({"default_route": "public"}, routes, lambda r: providers[r.name])
    message = [Message(role="user", content="go")]
    secure, public = await asyncio.gather(
        router.invoke(
            message, classification="cui", residency="secure", allowed_routes={"secure"}
        ),
        router.invoke(
            message, classification="unclassified", residency="public", allowed_routes={"public"}
        ),
    )
    assert secure.model == "secure"
    assert public.model == "public"
    assert providers["secure"].calls == providers["public"].calls == 1


@pytest.mark.asyncio
async def test_streaming_applies_all_request_policy_overrides():
    providers = {name: Provider("p", name) for name in ("secure", "public")}
    routes = [
        Route(
            "secure", "p", "secure", classification_max="cui", residency="secure", cost_per_1k=0.1
        ),
        Route(
            "public",
            "p",
            "public",
            classification_max="top_secret",
            residency="public",
            cost_per_1k=2.0,
        ),
    ]
    router = RoutingModule({"default_route": "public"}, routes, lambda r: providers[r.name])
    deltas = [
        delta
        async for delta in router.invoke_stream(
            [Message(role="user", content="go")],
            classification="cui",
            residency="secure",
            allowed_routes={"secure"},
            required_capabilities={"streaming"},
            remaining_budget_usd=1.0,
        )
    ]
    assert [delta.text for delta in deltas] == ["secure"]
    assert providers["secure"].stream_calls == 1
    assert providers["public"].stream_calls == 0


def test_route_rejects_unknown_classification_max():
    with pytest.raises(ArcLLMConfigError, match="classification_max"):
        Route("secure", "p", "secure", classification_max="restricted")
