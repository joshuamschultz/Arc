import pytest

from arcllm.modules.routing import Route, RoutingModule
from arcllm.types import LLMResponse, Message, Tool, Usage

_USAGE = Usage(input_tokens=1, output_tokens=1, total_tokens=2)


class Provider:
    def __init__(self, name: str, model: str) -> None:
        self.name, self.model_name, self.calls = name, model, 0

    async def invoke(self, messages, tools=None, **kwargs):
        self.calls += 1
        return LLMResponse(content="ok", usage=_USAGE, model=self.model_name, stop_reason="end_turn")

    def validate_config(self):
        return True

    async def close(self):
        return None


def build_router(**config):
    providers = {"default": Provider("p", "cheap"), "strong": Provider("p", "strong")}
    routes = [Route("default", "p", "cheap"), Route("strong", "p", "strong")]
    return RoutingModule({"default_route": "default", **config}, routes, lambda r: providers[r.name]), providers


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
    with pytest.raises(Exception, match="capabil"):
        await router.invoke(
            [Message(role="user", content="go")],
            tools=[Tool(name="x", description="x", parameters={})],
        )
    assert providers["default"].calls == 0
