"""Provider-neutral reconstruction of streamed ArcLLM responses."""

import pytest

from arcllm import (
    ArcLLMStreamProtocolError,
    Delta,
    StreamAccumulator,
    ToolCallDelta,
    Usage,
)


def test_accumulator_reconstructs_text_parallel_tools_usage_and_stop_reason() -> None:
    accumulator = StreamAccumulator(model="test-model")

    for delta in (
        Delta(text="I will "),
        Delta(tool_call=ToolCallDelta(index=1, id="call-b", name="second", arguments='{"b":')),
        Delta(tool_call=ToolCallDelta(index=0, id="call-a", name="first", arguments='{"a":')),
        Delta(text="do that."),
        Delta(tool_call=ToolCallDelta(index=1, arguments="2}")),
        Delta(tool_call=ToolCallDelta(index=0, arguments="1}")),
        Delta(usage=Usage(input_tokens=3, output_tokens=8, total_tokens=11)),
        Delta(stop_reason="tool_use"),
    ):
        accumulator.add(delta)

    response = accumulator.build()

    assert response.content == "I will do that."
    assert response.stop_reason == "tool_use"
    assert response.usage.total_tokens == 11
    assert [(call.id, call.name, call.arguments) for call in response.tool_calls] == [
        ("call-a", "first", {"a": 1}),
        ("call-b", "second", {"b": 2}),
    ]
    assert response.thinking is None


@pytest.mark.parametrize(
    "deltas",
    [
        [Delta(tool_call=ToolCallDelta(index=0, id="call-a", name="tool", arguments='{"a":'))],
        [Delta(tool_call=ToolCallDelta(index=0, id="call-a", name="tool", arguments="[]"))],
        [
            Delta(tool_call=ToolCallDelta(index=0, id="call-a", name="tool", arguments="{}")),
            Delta(tool_call=ToolCallDelta(index=0, id="call-b")),
        ],
        [
            Delta(tool_call=ToolCallDelta(index=0, id="call-a", name="tool", arguments="{}")),
            Delta(tool_call=ToolCallDelta(index=0, name="other")),
        ],
    ],
)
def test_accumulator_rejects_invalid_or_conflicting_tool_fragments(deltas: list[Delta]) -> None:
    accumulator = StreamAccumulator(model="test-model")
    for delta in deltas:
        accumulator.add(delta)

    with pytest.raises(ArcLLMStreamProtocolError, match="streamed tool call"):
        accumulator.build()


def test_delta_rejects_hidden_reasoning_payload() -> None:
    with pytest.raises(ValueError, match="reasoning"):
        Delta.model_validate({"text": "visible", "reasoning": "private chain of thought"})


def test_accumulator_preserves_terminal_metadata() -> None:
    accumulator = StreamAccumulator(model="test-model")
    accumulator.add(Delta(text="ok"))
    accumulator.add(Delta(stop_reason="end_turn", metadata={"request_signature": "sig"}))

    response = accumulator.build()

    assert response.metadata == {"request_signature": "sig"}


@pytest.mark.parametrize("fragments", [(), ("",), ("", ""), ("   ",)])
def test_accumulator_reads_absent_arguments_as_an_empty_object(
    fragments: tuple[str, ...],
) -> None:
    """A zero-argument tool call streams no argument text — that is not malformed.

    Providers emit no ``arguments`` delta (or an empty one) for a tool taking
    no parameters. Treating the empty accumulation as a JSON decode failure
    killed the whole run: one such call ended a 13-minute workflow node with
    "streamed tool call arguments are malformed".
    """
    accumulator = StreamAccumulator(model="test-model")
    accumulator.add(Delta(tool_call=ToolCallDelta(index=0, id="call-a", name="ping")))
    for fragment in fragments:
        accumulator.add(Delta(tool_call=ToolCallDelta(index=0, arguments=fragment)))

    response = accumulator.build()

    assert [(call.name, call.arguments) for call in response.tool_calls] == [("ping", {})]


def test_accumulator_still_rejects_genuinely_malformed_arguments() -> None:
    """Truncated or non-object JSON remains a protocol error, not a silent {}."""
    accumulator = StreamAccumulator(model="test-model")
    accumulator.add(
        Delta(tool_call=ToolCallDelta(index=0, id="call-a", name="tool", arguments='{"a":'))
    )

    with pytest.raises(ArcLLMStreamProtocolError, match="malformed"):
        accumulator.build()
