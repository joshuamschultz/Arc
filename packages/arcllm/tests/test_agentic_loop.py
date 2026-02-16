"""Step 4: Agentic Tool-Calling Loop — Script version of the notebook.

Validates ArcLLM's unified interface by running real API calls through
the Anthropic adapter in a complete agentic tool-calling loop.
"""

import asyncio
import os

import pytest

# Skip entire module if no API key — prevents collection crash in CI
if not os.environ.get("ANTHROPIC_API_KEY"):
    pytest.skip("ANTHROPIC_API_KEY not set — skipping live API tests", allow_module_level=True)

print("API key found.")

from arcllm import (
    AnthropicAdapter,
    LLMResponse,
    Message,
    TextBlock,
    Tool,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    load_provider_config,
)


async def main():
    # --- Setup ---
    config = load_provider_config("anthropic")
    print(f"Provider: {config.provider.api_format}")
    print(f"Default model: {config.provider.default_model}")
    print(f"Available models: {list(config.models.keys())}")

    MODEL = "claude-haiku-4-5-20251001"
    adapter = AnthropicAdapter(config, MODEL)
    print(f"\nAdapter name: {adapter.name}")
    print(f"Model: {adapter.model_name}")
    print(
        f"Model meta: context_window={adapter._model_meta.context_window}, "
        f"supports_tools={adapter._model_meta.supports_tools}"
    )

    # ==========================================
    # Test 1: Simple Text Response
    # ==========================================
    print("\n" + "=" * 60)
    print("TEST 1: Simple Text Response")
    print("=" * 60)

    messages = [
        Message(role="user", content="What is 2 + 2? Reply with just the number.")
    ]

    response = await adapter.invoke(messages, max_tokens=50)

    print(f"Type: {type(response).__name__}")
    print(f"Content: {response.content}")
    print(f"Stop reason: {response.stop_reason}")
    print(
        f"Usage: in={response.usage.input_tokens}, "
        f"out={response.usage.output_tokens}, "
        f"total={response.usage.total_tokens}"
    )
    print(f"Tool calls: {response.tool_calls}")
    print(f"Model: {response.model}")

    assert isinstance(response, LLMResponse)
    assert isinstance(response.content, str)
    assert response.stop_reason == "end_turn"
    assert isinstance(response.usage, Usage)
    assert response.usage.total_tokens > 0
    assert response.tool_calls == []
    print("\n>>> All assertions passed")

    # ==========================================
    # Test 2: Calculator Tool Loop
    # ==========================================
    print("\n" + "=" * 60)
    print("TEST 2: Calculator Tool Loop")
    print("=" * 60)

    calculator_tool = Tool(
        name="calculate",
        description="Evaluate a mathematical expression. Returns the numeric result.",
        parameters={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "The math expression to evaluate, e.g. '2 + 3 * 4'",
                }
            },
            "required": ["expression"],
        },
    )

    def execute_calculate(arguments: dict) -> str:
        expr = arguments["expression"]
        allowed = set("0123456789+-*/.() ")
        if not all(c in allowed for c in expr):
            return f"Error: unsafe expression '{expr}'"
        try:
            result = eval(expr)  # noqa: S307
            return str(result)
        except Exception as e:
            return f"Error: {e}"

    # Turn 1: Send math problem with calculator tool
    messages = [
        Message(
            role="system",
            content="You are a helpful assistant. Use the calculate tool for any math.",
        ),
        Message(role="user", content="What is 137 * 42 + 19?"),
    ]

    response_1 = await adapter.invoke(messages, tools=[calculator_tool], max_tokens=200)

    print(f"Stop reason: {response_1.stop_reason}")
    print(f"Content: {response_1.content}")
    print(f"Tool calls: {len(response_1.tool_calls)}")

    assert response_1.stop_reason == "tool_use"
    assert len(response_1.tool_calls) >= 1

    tool_call = response_1.tool_calls[0]
    print(f"\nTool call:")
    print(f"  id: {tool_call.id}")
    print(f"  name: {tool_call.name}")
    print(f"  arguments: {tool_call.arguments}")

    assert isinstance(tool_call, ToolCall)
    assert tool_call.name == "calculate"
    assert isinstance(tool_call.arguments, dict)
    print("\n>>> Turn 1 assertions passed")

    # Turn 2: Execute tool and send result back
    result = execute_calculate(tool_call.arguments)
    print(f"\nTool result: {result}")

    assistant_content = []
    if response_1.content:
        assistant_content.append(TextBlock(text=response_1.content))
    for tc in response_1.tool_calls:
        assistant_content.append(
            ToolUseBlock(id=tc.id, name=tc.name, arguments=tc.arguments)
        )

    messages.append(Message(role="assistant", content=assistant_content))
    messages.append(
        Message(
            role="tool",
            content=[ToolResultBlock(tool_use_id=tool_call.id, content=result)],
        )
    )

    response_2 = await adapter.invoke(messages, tools=[calculator_tool], max_tokens=200)

    print(f"Stop reason: {response_2.stop_reason}")
    print(f"Content: {response_2.content}")
    print(f"Tool calls: {response_2.tool_calls}")
    print(
        f"Usage: in={response_2.usage.input_tokens}, out={response_2.usage.output_tokens}"
    )

    assert response_2.stop_reason == "end_turn"
    assert response_2.content is not None
    assert response_2.tool_calls == []
    assert "5773" in response_2.content or "5,773" in response_2.content, (
        f"Expected 5773 in response: {response_2.content}"
    )
    print("\n>>> Calculator loop complete — all assertions passed")

    # ==========================================
    # Test 3: Search Tool Loop
    # ==========================================
    print("\n" + "=" * 60)
    print("TEST 3: Search Tool Loop")
    print("=" * 60)

    search_tool = Tool(
        name="web_search",
        description="Search the web for current information. Returns relevant search results.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"}
            },
            "required": ["query"],
        },
    )

    SEARCH_RESULTS = {
        "default": "Search result: According to recent data, the answer you're looking for is available. Here are the key findings: The topic has been extensively studied with conclusive results."
    }

    def execute_search(arguments: dict) -> str:
        query = arguments.get("query", "")
        return f"Search results for '{query}': {SEARCH_RESULTS['default']}"

    search_messages = [
        Message(
            role="user",
            content="Search for information about the Eiffel Tower's height and tell me what you find.",
        )
    ]

    search_response_1 = await adapter.invoke(
        search_messages, tools=[search_tool], max_tokens=300
    )

    print(f"Stop reason: {search_response_1.stop_reason}")
    print(f"Tool calls: {len(search_response_1.tool_calls)}")

    assert search_response_1.stop_reason == "tool_use"
    assert len(search_response_1.tool_calls) >= 1

    search_tc = search_response_1.tool_calls[0]
    print(f"Tool: {search_tc.name}, args: {search_tc.arguments}")
    assert search_tc.name == "web_search"

    search_result = execute_search(search_tc.arguments)
    print(f"Result: {search_result[:100]}...")

    search_assistant_content = []
    if search_response_1.content:
        search_assistant_content.append(TextBlock(text=search_response_1.content))
    for tc in search_response_1.tool_calls:
        search_assistant_content.append(
            ToolUseBlock(id=tc.id, name=tc.name, arguments=tc.arguments)
        )

    search_messages.append(Message(role="assistant", content=search_assistant_content))
    search_messages.append(
        Message(
            role="tool",
            content=[ToolResultBlock(tool_use_id=search_tc.id, content=search_result)],
        )
    )

    search_response_2 = await adapter.invoke(
        search_messages, tools=[search_tool], max_tokens=300
    )

    print(f"\nFinal stop reason: {search_response_2.stop_reason}")
    print(f"Final content: {search_response_2.content}")

    assert search_response_2.stop_reason == "end_turn"
    assert search_response_2.content is not None
    print("\n>>> Search loop complete — all assertions passed")

    # ==========================================
    # Test 4: Multi-Tool (Both Available)
    # ==========================================
    print("\n" + "=" * 60)
    print("TEST 4: Multi-Tool Agentic Loop")
    print("=" * 60)

    TOOL_EXECUTORS = {
        "calculate": execute_calculate,
        "web_search": execute_search,
    }

    async def run_agentic_loop(
        adapter: AnthropicAdapter,
        messages: list[Message],
        tools: list[Tool],
        max_turns: int = 5,
    ) -> LLMResponse:
        for turn in range(max_turns):
            resp = await adapter.invoke(messages, tools=tools, max_tokens=500)
            print(
                f"  Turn {turn + 1}: stop_reason={resp.stop_reason}, "
                f"tool_calls={len(resp.tool_calls)}"
            )

            if resp.stop_reason != "tool_use":
                return resp

            assistant_content = []
            if resp.content:
                assistant_content.append(TextBlock(text=resp.content))
            for tc in resp.tool_calls:
                assistant_content.append(
                    ToolUseBlock(id=tc.id, name=tc.name, arguments=tc.arguments)
                )
            messages.append(Message(role="assistant", content=assistant_content))

            tool_results = []
            for tc in resp.tool_calls:
                executor = TOOL_EXECUTORS.get(tc.name)
                if executor:
                    res = executor(tc.arguments)
                    print(f"    Tool '{tc.name}': {res[:80]}")
                else:
                    res = f"Error: unknown tool '{tc.name}'"
                tool_results.append(ToolResultBlock(tool_use_id=tc.id, content=res))

            messages.append(Message(role="tool", content=tool_results))

        raise RuntimeError(f"Agentic loop did not complete in {max_turns} turns")

    multi_messages = [
        Message(
            role="system",
            content="You have access to a calculator and web search. Use the appropriate tool.",
        ),
        Message(
            role="user",
            content="What is 256 * 789? Use the calculate tool.",
        ),
    ]

    print("Running multi-tool agentic loop...")
    final_response = await run_agentic_loop(
        adapter, multi_messages, [calculator_tool, search_tool]
    )

    print(f"\nFinal response:")
    print(f"  Content: {final_response.content}")
    print(f"  Stop reason: {final_response.stop_reason}")
    print(
        f"  Usage: in={final_response.usage.input_tokens}, "
        f"out={final_response.usage.output_tokens}"
    )

    assert final_response.stop_reason == "end_turn"
    assert final_response.content is not None
    assert "201984" in final_response.content or "201,984" in final_response.content, (
        f"Expected 201984 in: {final_response.content}"
    )
    print("\n>>> Multi-tool loop complete — all assertions passed")

    # ==========================================
    # Summary & Type Verification
    # ==========================================
    print("\n" + "=" * 60)
    print("SUMMARY: Type Verification")
    print("=" * 60)

    all_responses = [
        response,
        response_2,
        search_response_1,
        search_response_2,
        final_response,
    ]

    print("Type verification across all responses:")
    for i, r in enumerate(all_responses):
        assert isinstance(r, LLMResponse), f"Response {i} is not LLMResponse"
        assert isinstance(r.usage, Usage), f"Response {i} usage is not Usage"
        assert isinstance(r.model, str), f"Response {i} model is not str"
        assert isinstance(r.stop_reason, str), f"Response {i} stop_reason is not str"
        assert isinstance(r.tool_calls, list), f"Response {i} tool_calls is not list"
        for tc in r.tool_calls:
            assert isinstance(tc, ToolCall), "Tool call is not ToolCall"
            assert isinstance(tc.arguments, dict), "Tool call arguments is not dict"
        print(
            f"  Response {i}: OK "
            f"(stop={r.stop_reason}, tools={len(r.tool_calls)}, "
            f"tokens={r.usage.total_tokens})"
        )

    total_tokens = sum(r.usage.total_tokens for r in all_responses)
    print(f"\nAll {len(all_responses)} responses are correctly typed")
    print(f"Total tokens used: {total_tokens}")

    # Cleanup
    await adapter.close()
    print("\nAdapter closed. Step 4 complete!")


if __name__ == "__main__":
    asyncio.run(main())
