"""A photo sent to an agent reaches the model as an image, not as a filename.

SPEC-065 REQ-301/REQ-316. ``test_inbound_wiring.py`` proves the gateway half:
a real photo lands in the workspace and the agent is handed a reference. This
file proves the other half, and the two together are the whole path — the seam
between them (``deliver_message``) is where a message stopped being a message
and became a string.

Nothing is mocked between ``deliver_message`` and the loop call. What is faked
is only the **model wire**: ``arcrun.run_async`` / ``run_stream`` record the
messages they were handed. Every layer in between — the part translator, the
session manager writing a real jsonl, ``wire_messages`` — is real, because a
per-layer mock here would prove only that the mock was called.

Two properties are asserted together, and they pull in opposite directions:

* the **model** receives the bytes, as an image block it can actually look at;
* the **session log** receives a reference, and stays kilobytes forever.

Satisfying either one alone is a regression: inlining base64 into history
passes the first and fails the second, and the shipped behaviour (a readable
line naming the file) passed the second while the model never saw the photo.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import arcrun
import pytest
from arcrun import TurnEndEvent

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
)

pytestmark = pytest.mark.asyncio

_SENDER_DID = "did:arc:user:alice"
_SESSION_KEY = "telegram-8293394811"
_IMAGE_REF = "inbox/2026-08-16/141522-alice-photo.jpg"
_PDF_REF = "inbox/2026-08-16/141600-alice-invoice.pdf"

# Real-ish bytes: a JPEG SOI marker so nothing downstream can claim it rejected
# the file for not looking like an image.
_IMAGE_BYTES = b"\xff\xd8\xff\xe0" + b"ARC-PHOTO-PAYLOAD" * 64
_PDF_BYTES = b"%PDF-1.7\nARC-DOC-PAYLOAD\n%%EOF\n"


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    for ref, payload in ((_IMAGE_REF, _IMAGE_BYTES), (_PDF_REF, _PDF_BYTES)):
        target = ws / ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return ws


def _config(tmp_path: Path, workspace: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="media-agent", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=100000),
    )


@pytest.fixture(autouse=True)
def _no_real_model() -> Any:
    """The model wire is the only thing faked; nothing below it is."""
    with patch("arcagent.core.model_manager.load_eval_model") as load:
        load.return_value = MagicMock(close=AsyncMock())
        yield load


async def _started_agent(tmp_path: Path, workspace: Path) -> ArcAgent:
    agent = ArcAgent(config=_config(tmp_path, workspace))
    await agent.startup()
    return agent


def _photo_parts() -> list[dict[str, Any]]:
    """A photo with a caption — exactly what the Telegram adapter hands up."""
    return [
        {"kind": "text", "text": "what do you make of this?"},
        {
            "kind": "image",
            "mime": "image/jpeg",
            "declared_name": "photo.jpg",
            "ref": _IMAGE_REF,
        },
    ]


def _document_parts() -> list[dict[str, Any]]:
    return [
        {
            "kind": "file",
            "mime": "application/pdf",
            "declared_name": "invoice.pdf",
            "ref": _PDF_REF,
        },
    ]


class _RecordingRunAsync:
    """Stands in for the model wire, recording the messages it was handed."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        handle = MagicMock()
        handle.result = AsyncMock(return_value=MagicMock(content="ok"))
        return handle

    @property
    def messages(self) -> list[Any]:
        assert self.calls, "the loop was never called — no turn was opened"
        return list(self.calls[-1]["messages"])


def _image_blocks(messages: list[Any]) -> list[arcrun.ImageBlock]:
    blocks: list[arcrun.ImageBlock] = []
    for message in messages:
        content = message.content
        if isinstance(content, list):
            blocks.extend(b for b in content if isinstance(b, arcrun.ImageBlock))
    return blocks


async def test_a_photo_reaches_the_model_as_an_image_block(
    tmp_path: Path, workspace: Path
) -> None:
    """The whole point: Claude is handed the picture, not the path to it."""
    agent = await _started_agent(tmp_path, workspace)
    wire = _RecordingRunAsync()
    try:
        with patch("arcagent.core.agent_dispatch.arcrun.run_async", new=wire):
            await agent.deliver_message(
                caller_did=_SENDER_DID,
                message="what do you make of this?",
                session_key=_SESSION_KEY,
                parts=_photo_parts(),
            )
    finally:
        await agent.shutdown()

    images = _image_blocks(wire.messages)
    assert len(images) == 1, (
        "the model was handed no image block — the photo reached it as a "
        "filename, which is what a text-only model sees as noise"
    )
    assert base64.b64decode(images[0].source) == _IMAGE_BYTES
    assert images[0].media_type == "image/jpeg"


async def test_the_caption_still_rides_with_the_photo(tmp_path: Path, workspace: Path) -> None:
    """A caption is a part like any other; it must not be dropped for the image."""
    agent = await _started_agent(tmp_path, workspace)
    wire = _RecordingRunAsync()
    try:
        with patch("arcagent.core.agent_dispatch.arcrun.run_async", new=wire):
            await agent.deliver_message(
                caller_did=_SENDER_DID,
                message="what do you make of this?",
                session_key=_SESSION_KEY,
                parts=_photo_parts(),
            )
    finally:
        await agent.shutdown()

    text = "".join(
        block.text
        for message in wire.messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, arcrun.TextBlock)
    )
    assert "what do you make of this?" in text


async def test_the_session_log_keeps_the_reference_not_the_bytes(
    tmp_path: Path, workspace: Path
) -> None:
    """Fixing the model path by inlining base64 into history is not a fix."""
    agent = await _started_agent(tmp_path, workspace)
    wire = _RecordingRunAsync()
    try:
        with patch("arcagent.core.agent_dispatch.arcrun.run_async", new=wire):
            await agent.deliver_message(
                caller_did=_SENDER_DID,
                message="what do you make of this?",
                session_key=_SESSION_KEY,
                parts=_photo_parts(),
            )
    finally:
        await agent.shutdown()

    written = (workspace / "sessions" / f"{_SESSION_KEY}.jsonl").read_text(encoding="utf-8")
    assert _IMAGE_REF in written
    assert base64.b64encode(_IMAGE_BYTES).decode("ascii") not in written


async def test_a_document_is_named_for_the_model_never_inlined(
    tmp_path: Path, workspace: Path
) -> None:
    """A PDF is not an image block: naming it costs tokens, inlining costs megabytes."""
    agent = await _started_agent(tmp_path, workspace)
    wire = _RecordingRunAsync()
    try:
        with patch("arcagent.core.agent_dispatch.arcrun.run_async", new=wire):
            await agent.deliver_message(
                caller_did=_SENDER_DID,
                message="here",
                session_key=_SESSION_KEY,
                parts=_document_parts(),
            )
    finally:
        await agent.shutdown()

    assert _image_blocks(wire.messages) == []
    text = "".join(
        block.text
        for message in wire.messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, arcrun.TextBlock)
    )
    assert "invoice.pdf" in text and _PDF_REF in text


async def test_a_photo_arriving_mid_turn_reaches_the_model_too(
    tmp_path: Path, workspace: Path
) -> None:
    """The agent is slow; the second photo joins the live run rather than opening one.

    This is the ordinary case on a chat platform, not an edge: a turn takes
    tens of seconds and a person sends the picture they were about to send.
    Delivering it as a filename here would make the fix hold only for a photo
    that happened to arrive while the agent was idle.
    """
    agent = await _started_agent(tmp_path, workspace)
    injected: list[Any] = []

    class _LiveHandle:
        async def follow_up(self, caller_did: str, message: Any) -> None:
            injected.append(message)

        async def steer(self, caller_did: str, message: Any) -> None:
            injected.append(message)

    try:
        agent._run_coordinator.register(_SESSION_KEY, _LiveHandle(), interactive=True)
        await agent.deliver_message(
            caller_did=_SENDER_DID,
            message="what do you make of this?",
            session_key=_SESSION_KEY,
            parts=_photo_parts(),
        )
    finally:
        await agent.shutdown()

    assert injected, "the message never reached the live run"
    blocks = injected[-1]
    assert isinstance(blocks, list), (
        "the mid-turn photo was injected as a string — the live run sees a "
        "filename where the idle path sees the picture"
    )
    images = [b for b in blocks if isinstance(b, arcrun.ImageBlock)]
    assert len(images) == 1
    assert base64.b64decode(images[0].source) == _IMAGE_BYTES


async def test_a_text_only_message_still_travels_as_text(
    tmp_path: Path, workspace: Path
) -> None:
    """The many surfaces that only ever have words take the path they always did."""
    agent = await _started_agent(tmp_path, workspace)
    calls: list[dict[str, Any]] = []

    async def _fake_run_stream(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)

        async def _gen() -> Any:
            yield TurnEndEvent(final_text="done", tool_calls_made=0)

        return _gen()

    try:
        with patch(
            "arcagent.core.agent_dispatch.arcrun.run_stream", side_effect=_fake_run_stream
        ):
            session = await agent.session("text-only")
            async for _ in agent.run("just words", session=session):
                pass
    finally:
        await agent.shutdown()

    assert calls[-1]["messages"][-1].content == "just words"
