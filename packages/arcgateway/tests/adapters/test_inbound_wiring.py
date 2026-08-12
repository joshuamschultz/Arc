"""T-940 — the media pipeline is wired: a real photo reaches the agent as a ref.

SPEC-065 REQ-296/297/298/299/308, COMP-002/COMP-005/COMP-013.

**This is the file that proves the pipeline is not dead.** Phases 1 and 2 built
``MediaStore``, the part vocabulary and ``PartTranslator``, and as of this
commit not one of them has a production caller: they are correct, tested, and
unreachable. Every per-layer unit test in the repo passes while a photo sent to
an agent still produces nothing at all. That is the exact failure mode the repo
has recorded before — per-layer mocks let every layer pass while the end-to-end
path was dead — so nothing here is mocked between the adapter and the agent.

What is faked is only the **platform wire**: the Telegram/Slack/Mattermost
payload object and the callable that fetches its bytes. Everything after that
is real — the adapter's own inbound handler, the real ``SessionRouter``, the
real ``MediaStore`` writing to a real temporary workspace. The assertions are
made on the file that exists on disk and on the object the agent is handed.

Two properties are asserted in every case, and they pull in opposite directions
on purpose:

* the **bytes** are on disk, inside the workspace inbox, complete; and
* the **envelope** carries a reference and no bytes anywhere in it.

Security cases (REQ-298, REQ-299): the sender does not choose where their file
lands and cannot talk its way past the ceiling. A hostile declared filename is
retained as metadata and contributes nothing to the path, and the ceiling is
enforced against the bytes that actually arrived, not against the size the
payload claimed.

Wiring this suite assumes of the green implementation, all in ``_build_gateway``
and ``_install_wire`` so a different choice is a one-place change:

    SessionRouter(executor=..., adapter=..., media_store_for=lambda did: MediaStore(...))
    the executor/agent receives an envelope exposing ``.parts``
    MediaPart.ref is a workspace-relative (or absolute) path to the stored file
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcgateway.media_store import MediaStore
from arcgateway.session import SessionRouter

pytestmark = pytest.mark.asyncio

_AGENT_DID = "did:arc:agent:media"
_MAX_BYTES = 8 * 1024 * 1024

# A unique marker so "the bytes did not travel in the envelope" can be asserted
# by searching the serialised envelope for it, raw or base64.
_MARKER = b"ARC-MEDIA-MARKER-9f2c"
_IMAGE_BYTES = b"\x89PNG\r\n\x1a\n" + _MARKER + b"\x00" * 64
_FILE_BYTES = b"%PDF-1.7\n" + _MARKER + b"\n%%EOF\n"

# <workspace>/inbox/<YYYY-MM-DD>/<hhmmss>-<sender>-<stem>.<ext>
_DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GATEWAY_PREFIX = re.compile(r"^\d{6}-")


# ---------------------------------------------------------------------------
# The gateway, real
# ---------------------------------------------------------------------------


class _RecordingAgent:
    """Stands in for the agent: records every envelope delivered to it."""

    def __init__(self) -> None:
        self.received: list[Any] = []

    async def run(self, event: Any) -> Any:
        self.received.append(event)
        return self._stream()

    async def _stream(self) -> Any:
        from arcgateway.executor import Delta

        yield Delta(kind="done", content="", is_final=True, turn_id="t")


class _CapturingAdapter:
    """Outbound side: records what the gateway sent back to the channel."""

    name = "web"
    agent_did = ""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, target: Any, message: Any, **_: Any) -> None:
        self.sent.append(str(message))


@dataclass
class _Gateway:
    router: SessionRouter
    agent: _RecordingAgent
    outbound: _CapturingAdapter
    workspace: Path

    def inbox_files(self) -> list[Path]:
        inbox = self.workspace / "inbox"
        if not inbox.exists():
            return []
        return sorted(path for path in inbox.rglob("*") if path.is_file())

    def delivered(self) -> Any:
        assert self.agent.received, "no envelope was delivered to the agent at all"
        return self.agent.received[-1]

    def parts(self) -> list[Any]:
        envelope = self.delivered()
        parts = getattr(envelope, "parts", None)
        assert parts is not None, (
            f"the delivered envelope has no `parts` — it is still the text-only "
            f"InboundEvent ({type(envelope).__name__})"
        )
        return list(parts)


def _build_gateway(workspace: Path, *, max_bytes: int = _MAX_BYTES) -> _Gateway:
    """The real router and the real store, over a real temporary workspace."""
    agent = _RecordingAgent()
    outbound = _CapturingAdapter()
    router = SessionRouter(
        executor=agent,
        adapter=outbound,
        # Per-agent resolver, not a shared store: the router serves the whole
        # fleet but a workspace belongs to one agent (ADR-029).
        media_store_for=lambda _agent_did: MediaStore(
            workspace=workspace, max_bytes=max_bytes
        ),
    )
    return _Gateway(router=router, agent=agent, outbound=outbound, workspace=workspace)


# ---------------------------------------------------------------------------
# The platform wire, faked
# ---------------------------------------------------------------------------


@dataclass
class _Wire:
    """One platform's fake wire: the bytes it serves and the replies it took."""

    data: bytes
    bot: Any = None
    replies: list[str] = field(default_factory=list)

    def told_the_sender(self) -> str:
        """Everything the platform was asked to say back on this channel."""
        texts = list(self.replies)
        if self.bot is not None:
            for call in getattr(self.bot.send_message, "await_args_list", []):
                texts.append(str(call.kwargs.get("text", "")) + " ".join(map(str, call.args)))
        return " ".join(texts)


def _telegram_wire(data: bytes) -> _Wire:
    """A Telegram Bot whose get_file/download returns ``data`` and records replies."""
    wire = _Wire(data=data)

    handle = MagicMock()
    handle.file_path = "photos/file_1.jpg"
    handle.file_size = len(data)
    handle.file_id = "wire-file-id"
    handle.download_as_bytearray = AsyncMock(return_value=bytearray(data))

    async def _to_memory(out: Any, *_: Any, **__: Any) -> None:
        out.write(data)

    handle.download_to_memory = AsyncMock(side_effect=_to_memory)

    async def _to_drive(path: Any = None, *_: Any, **__: Any) -> Any:
        target = Path(str(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    handle.download_to_drive = AsyncMock(side_effect=_to_drive)

    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=handle)
    bot.send_message = AsyncMock()
    bot.send_chat_action = AsyncMock()
    wire.bot = bot
    return wire


def _telegram_adapter(wire: _Wire, on_message: Any) -> Any:
    """The real TelegramAdapter, in-tree if it has moved, distribution if not."""
    adapter_cls = _import_adapter("telegram", "TelegramAdapter")
    adapter = adapter_cls(
        bot_token="test-token-abc",
        allowed_user_ids=[42],
        on_message=on_message,
        agent_did=_AGENT_DID,
    )
    adapter._bot_id = 999
    application = MagicMock()
    application.bot = wire.bot
    adapter._application = application
    return adapter


def _import_adapter(platform: str, class_name: str) -> Any:
    """Prefer the in-tree folder (T-940); fall back to the distribution.

    The fallback is deliberate: today it makes this suite drive the *current*
    adapter and fail on the missing media part — the defect reproduced — rather
    than failing at import and proving only that a folder does not exist yet.
    """
    import importlib

    for module_path in (
        f"arcgateway.adapters.{platform}",
        f"arcgateway.adapters.{platform}.adapter",
        f"arcgateway_{platform}.adapter",
    ):
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            continue
        found = getattr(module, class_name, None)
        if found is not None:
            return found
    pytest.fail(f"no {class_name} importable for platform {platform!r}")


def _telegram_update(
    *, text: str | None = None, caption: str | None = None, photo: bool = False,
    document: str | None = None, declared_size: int | None = None,
) -> Any:
    """A Telegram Update as python-telegram-bot would deliver it."""
    update = MagicMock()
    update.update_id = 7
    message = update.effective_message
    message.text = text
    message.caption = caption
    message.photo = []
    message.document = None
    message.audio = None
    message.voice = None
    message.video = None

    if photo:
        size = MagicMock()
        size.file_id = "photo-file-id"
        size.file_unique_id = "photo-uid"
        size.file_size = declared_size if declared_size is not None else len(_IMAGE_BYTES)
        size.width, size.height = 800, 600
        message.photo = [size]

    if document is not None:
        doc = MagicMock()
        doc.file_id = "doc-file-id"
        doc.file_unique_id = "doc-uid"
        doc.file_name = document
        doc.mime_type = "application/pdf"
        doc.file_size = declared_size if declared_size is not None else len(_FILE_BYTES)
        message.document = doc

    message.chat.id = 42
    message.chat.type = "private"
    update.effective_chat.id = 42
    update.effective_user.id = 42
    update.effective_user.first_name = "Op"
    update.effective_user.username = "op"
    update.effective_user.is_bot = False
    return update


async def _deliver(adapter: Any, update: Any, wire: _Wire) -> None:
    """Drive the adapter's real inbound handler, then let the router settle."""
    context = MagicMock()
    context.bot = wire.bot
    await adapter._handle_update(update, context=context)
    for _ in range(20):
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------


def _media_parts(parts: list[Any], kind: str) -> list[Any]:
    return [part for part in parts if getattr(part, "kind", None) == kind]


def _resolve_ref(workspace: Path, ref: str) -> Path:
    candidate = Path(ref)
    return candidate if candidate.is_absolute() else workspace / candidate


def _assert_no_bytes_in_envelope(envelope: Any) -> None:
    """The bytes must not have ridden along — raw or base64."""
    import base64

    dumped = (
        envelope.model_dump_json()
        if hasattr(envelope, "model_dump_json")
        else repr(envelope)
    )
    assert _MARKER.decode() not in dumped, (
        "the artefact's bytes travelled inside the envelope — they belong in "
        "the workspace, not in the queue, the session log or the prompt"
    )
    assert base64.b64encode(_MARKER).decode().rstrip("=") not in dumped, (
        "the artefact's bytes travelled inside the envelope, base64-encoded"
    )


def _assert_gateway_composed_the_path(workspace: Path, stored: Path) -> None:
    """REQ-298: the gateway composed this path, not the sender."""
    assert stored.parent.parent == workspace / "inbox", (
        f"{stored} is not under the workspace inbox"
    )
    assert _DAY_DIR.match(stored.parent.name), (
        f"{stored.parent.name!r} is not the gateway's <YYYY-MM-DD> day directory"
    )
    assert _GATEWAY_PREFIX.match(stored.name), (
        f"{stored.name!r} does not start with the gateway's <hhmmss>- prefix — "
        "the sender influenced the filename beyond the sanitised stem"
    )


# ---------------------------------------------------------------------------
# Inbound image — the canary, end to end
# ---------------------------------------------------------------------------


async def test_a_photo_is_written_to_the_workspace_inbox(tmp_path: Path) -> None:
    """The bytes land on disk, complete, inside the agent's workspace (REQ-297)."""
    gateway = _build_gateway(tmp_path)
    wire = _telegram_wire(_IMAGE_BYTES)
    adapter = _telegram_adapter(wire, gateway.router.handle)

    await _deliver(adapter, _telegram_update(photo=True), wire)

    files = gateway.inbox_files()
    assert len(files) == 1, (
        f"a photo produced {len(files)} files in the workspace inbox, expected 1 "
        f"— the media pipeline has no caller"
    )
    assert files[0].read_bytes() == _IMAGE_BYTES, "the stored bytes are not the sent bytes"
    _assert_gateway_composed_the_path(tmp_path, files[0])


async def test_a_photo_reaches_the_agent_as_a_reference(tmp_path: Path) -> None:
    """The agent is handed an image part pointing at the stored file (REQ-296/301)."""
    gateway = _build_gateway(tmp_path)
    wire = _telegram_wire(_IMAGE_BYTES)
    adapter = _telegram_adapter(wire, gateway.router.handle)

    await _deliver(adapter, _telegram_update(photo=True), wire)

    images = _media_parts(gateway.parts(), "image")
    assert images, (
        "the agent received no image part — a photo still produces nothing it can use"
    )

    stored = _resolve_ref(tmp_path, images[0].ref)
    assert stored.is_file(), f"the media reference {images[0].ref!r} points at no file"
    assert stored.read_bytes() == _IMAGE_BYTES
    _assert_no_bytes_in_envelope(gateway.delivered())


async def test_a_document_reaches_the_agent_as_a_file_part(tmp_path: Path) -> None:
    """Not only photos: a document is a file part with its declared name kept."""
    gateway = _build_gateway(tmp_path)
    wire = _telegram_wire(_FILE_BYTES)
    adapter = _telegram_adapter(wire, gateway.router.handle)

    await _deliver(adapter, _telegram_update(document="quarterly report.pdf"), wire)

    files = _media_parts(gateway.parts(), "file")
    assert files, "a document produced no file part"
    assert files[0].declared_name == "quarterly report.pdf", (
        "the sender-supplied filename was not retained as metadata (REQ-298)"
    )
    assert _resolve_ref(tmp_path, files[0].ref).read_bytes() == _FILE_BYTES


async def test_a_captioned_photo_produces_both_parts(tmp_path: Path) -> None:
    """Text and media travel in one envelope, as an ordered list (REQ-296).

    The order is the implementation's to choose; that it is a *stable* order
    is not, because the agent reads the parts as the sender composed them.
    """
    gateway = _build_gateway(tmp_path)
    wire = _telegram_wire(_IMAGE_BYTES)
    adapter = _telegram_adapter(wire, gateway.router.handle)

    await _deliver(
        adapter, _telegram_update(photo=True, caption="look at this chart"), wire
    )
    first = [getattr(part, "kind", None) for part in gateway.parts()]

    texts = [part for part in gateway.parts() if getattr(part, "kind", None) == "text"]
    assert texts, "the caption was dropped — text is a part like any other"
    assert texts[0].text == "look at this chart"
    assert _media_parts(gateway.parts(), "image"), "the photo was dropped"
    assert len(first) == 2, f"expected exactly a text part and an image part, got {first}"

    # Same message again: the ordering must not be incidental.
    second_gateway = _build_gateway(tmp_path / "second")
    second_wire = _telegram_wire(_IMAGE_BYTES)
    second_adapter = _telegram_adapter(second_wire, second_gateway.router.handle)
    await _deliver(
        second_adapter,
        _telegram_update(photo=True, caption="look at this chart"),
        second_wire,
    )
    second = [getattr(part, "kind", None) for part in second_gateway.parts()]
    assert first == second, f"part order is not stable: {first} then {second}"


async def test_plain_text_still_arrives_as_a_single_text_part(tmp_path: Path) -> None:
    """REQ-315: the path that works today keeps working, in the new vocabulary."""
    gateway = _build_gateway(tmp_path)
    wire = _telegram_wire(_IMAGE_BYTES)
    adapter = _telegram_adapter(wire, gateway.router.handle)

    await _deliver(adapter, _telegram_update(text="hello"), wire)

    parts = gateway.parts()
    assert [getattr(part, "kind", None) for part in parts] == ["text"]
    assert parts[0].text == "hello"
    assert gateway.inbox_files() == [], "a text message wrote a file to the inbox"


# ---------------------------------------------------------------------------
# Security — the sender chooses neither the path nor the ceiling
# ---------------------------------------------------------------------------


async def test_a_hostile_declared_filename_cannot_choose_where_bytes_land(
    tmp_path: Path,
) -> None:
    """REQ-298 over a real platform payload, not a direct MediaStore call.

    The traversal is only interesting if it survives the whole route: the
    adapter reads it off the wire, hands it up, and the gateway composes the
    path. Anywhere on that route that treats the declared name as a path is a
    write outside the workspace.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    canary = tmp_path / "outside.txt"
    canary.write_text("untouched", encoding="utf-8")

    gateway = _build_gateway(workspace)
    wire = _telegram_wire(_FILE_BYTES)
    adapter = _telegram_adapter(wire, gateway.router.handle)

    hostile = "../../../../outside.txt"
    await _deliver(adapter, _telegram_update(document=hostile), wire)

    assert canary.read_text(encoding="utf-8") == "untouched", (
        "a declared filename escaped the workspace and overwrote a file outside it"
    )
    files = gateway.inbox_files()
    assert len(files) == 1, f"expected the artefact inside the inbox, found {files}"
    _assert_gateway_composed_the_path(workspace, files[0])
    assert "outside.txt" != files[0].name

    parts = _media_parts(gateway.parts(), "file")
    assert parts and parts[0].declared_name == hostile, (
        "the hostile name should be retained verbatim as metadata — it is "
        "evidence, and only its use as a path is forbidden"
    )


async def test_the_ceiling_is_enforced_against_the_bytes_not_the_claim(
    tmp_path: Path,
) -> None:
    """REQ-299. A payload that under-reports its size is the interesting case.

    The size in the payload is remote input. If the ceiling is checked against
    it, any sender can put any artefact into the workspace by claiming it is
    small — so the check has to happen against the bytes that arrived.
    """
    gateway = _build_gateway(tmp_path, max_bytes=64)
    oversized = b"A" * 4096
    wire = _telegram_wire(oversized)
    adapter = _telegram_adapter(wire, gateway.router.handle)

    await _deliver(
        adapter, _telegram_update(document="small.bin", declared_size=8), wire
    )

    assert gateway.inbox_files() == [], (
        "an artefact over the ceiling was written to the workspace because the "
        "payload claimed it was small"
    )


async def test_an_oversized_artefact_is_reported_on_the_channel_it_arrived_on(
    tmp_path: Path,
) -> None:
    """REQ-299: refusing silently is the failure mode, not the fix."""
    gateway = _build_gateway(tmp_path, max_bytes=64)
    wire = _telegram_wire(b"A" * 4096)
    adapter = _telegram_adapter(wire, gateway.router.handle)

    await _deliver(adapter, _telegram_update(document="big.bin"), wire)

    said = (wire.told_the_sender() + " " + " ".join(gateway.outbound.sent)).lower()
    assert said.strip(), "the artefact was refused and the sender was never told"
    assert any(word in said for word in ("large", "big", "size", "limit", "ceiling")), (
        f"the sender was told something, but not that the file was too large: {said!r}"
    )


async def test_the_adapter_cannot_choose_the_storage_path(tmp_path: Path) -> None:
    """REQ-310: path composition is the gateway's, whatever the adapter offers.

    A compromised or merely sloppy adapter is inside the trust boundary for its
    own platform, not for the workspace layout.
    """
    gateway = _build_gateway(tmp_path)
    wire = _telegram_wire(_FILE_BYTES)
    adapter = _telegram_adapter(wire, gateway.router.handle)

    # The platform hands up an absolute path as the filename.
    await _deliver(adapter, _telegram_update(document="/etc/arc/owned.conf"), wire)

    files = gateway.inbox_files()
    assert len(files) == 1
    _assert_gateway_composed_the_path(tmp_path, files[0])
    assert not Path("/etc/arc/owned.conf").exists()


# ---------------------------------------------------------------------------
# Every platform, not only the one with the reported defect
# ---------------------------------------------------------------------------


async def test_slack_delivers_an_inbound_file_to_the_agent(tmp_path: Path) -> None:
    """REQ-308/COMP-005: a Slack file share reaches the agent as a media part.

    The wire fake is permissive on purpose — it answers whichever download
    surface the adapter reaches for (``client.files_info`` plus an HTTP GET).
    If the green implementation fetches differently, extend the fake; the
    assertion below is about the outcome, not the mechanism.
    """
    gateway = _build_gateway(tmp_path)
    adapter_cls = _import_adapter("slack", "SlackAdapter")
    adapter = adapter_cls(
        bot_token="xoxb-test-token",
        app_token="xapp-test-token",
        allowed_user_ids=["U123"],
        on_message=gateway.router.handle,
        agent_did=_AGENT_DID,
        dedup_db_path=tmp_path / "dedup.db",
    )
    _install_permissive_http(adapter, _FILE_BYTES)

    await adapter._handle_inbound(
        {
            "user": "U123",
            "channel": "C999",
            "text": "here is the deck",
            "client_msg_id": "m-1",
            "files": [
                {
                    "id": "F1",
                    "name": "deck.pdf",
                    "mimetype": "application/pdf",
                    "size": len(_FILE_BYTES),
                    "url_private_download": "https://files.slack.com/F1/deck.pdf",
                }
            ],
        }
    )
    for _ in range(20):
        await asyncio.sleep(0.01)

    assert _media_parts(gateway.parts(), "file"), (
        "a Slack file share produced no media part — the adapter still reads text only"
    )
    assert gateway.inbox_files(), "a Slack file share wrote nothing to the workspace"


async def test_mattermost_delivers_an_inbound_file_to_the_agent(tmp_path: Path) -> None:
    """REQ-308/COMP-005: a Mattermost post with an attachment reaches the agent."""
    import json

    gateway = _build_gateway(tmp_path)
    adapter_cls = _import_adapter("mattermost", "MattermostAdapter")
    adapter = adapter_cls(
        server_url="https://mm.test",
        bot_token="mm-token",
        on_message=gateway.router.handle,
        agent_did=_AGENT_DID,
    )
    _install_permissive_http(adapter, _FILE_BYTES)

    post = {
        "id": "p1",
        "channel_id": "c1",
        "user_id": "u1",
        "message": "attached",
        "file_ids": ["f1"],
        "metadata": {
            "files": [
                {
                    "id": "f1",
                    "name": "notes.pdf",
                    "mime_type": "application/pdf",
                    "size": len(_FILE_BYTES),
                }
            ]
        },
    }
    await adapter._handle_ws_message(
        json.dumps({"event": "posted", "data": {"post": json.dumps(post)}})
    )
    for _ in range(20):
        await asyncio.sleep(0.01)

    assert _media_parts(gateway.parts(), "file"), (
        "a Mattermost attachment produced no media part — the adapter still "
        "reads post.message only"
    )
    assert gateway.inbox_files(), "a Mattermost attachment wrote nothing to the workspace"


def _install_permissive_http(adapter: Any, data: bytes) -> None:
    """Answer any download the adapter attempts with ``data``.

    Covers the three surfaces these two adapters plausibly use: the Slack
    ``AsyncWebClient``, an ``aiohttp`` session, and a plain awaited GET.
    """

    class _Body:
        """The streaming half of an aiohttp response (see base.read_bounded)."""

        async def iter_chunked(self, _size: int) -> Any:
            yield data

    class _Response:
        status = 200
        status_code = 200
        content = _Body()
        ok = True

        async def read(self) -> bytes:
            return data

        async def aread(self) -> bytes:
            return data

        def raise_for_status(self) -> None:
            return None

        async def json(self) -> dict[str, Any]:
            return {"ok": True, "file": {"url_private_download": "https://files.test/f"}}

        async def __aenter__(self) -> _Response:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

    class _Session:
        def get(self, *_: Any, **__: Any) -> _Response:
            return _Response()

        def post(self, *_: Any, **__: Any) -> _Response:
            return _Response()

        closed = False

        async def close(self) -> None:
            return None

    adapter._http_session = _Session()

    client = MagicMock()
    client.files_info = AsyncMock(
        return_value={
            "ok": True,
            "file": {
                "id": "F1",
                "name": "deck.pdf",
                "mimetype": "application/pdf",
                "size": len(data),
                "url_private_download": "https://files.slack.com/F1/deck.pdf",
            },
        }
    )
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1.0"})
    app = MagicMock()
    app.client = client
    adapter._app = app
