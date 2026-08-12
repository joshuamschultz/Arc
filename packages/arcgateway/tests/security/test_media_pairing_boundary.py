"""T-947 — pairing is the authorization boundary for media (SPEC-065 REQ-305).

COMP-007 (custody) and COMP-014 (pairing). Two properties that pull in
opposite directions, which is why they are asserted in one file:

* **An approved channel is not re-approved per artefact.** The operator
  already authorised this channel — once, deliberately, with
  ``arc gateway pair approve``. Asking a human to confirm every photo that
  arrives on it is gate fatigue, and gate fatigue is how real approvals get
  rubber-stamped. The gate that exists in Arc is per *tool call* inside the
  agent (``arcagent.tools.human_gate.HumanGate.request``, reached only from
  ``tool_registry`` on a forbidden trifecta composition); no inbound artefact
  may reach it.

* **An unpaired channel gets nothing at all.** ``packages/arcgateway/CLAUDE.md``:
  *No pairing → no agent response.* Attaching a file must not become a way to
  reach an agent, or to put bytes on the operator's disk, without pairing. The
  stranger's photo must produce no run, no stored artefact, no download, and
  no reply beyond the pairing flow itself.

Nothing between the platform wire and the agent is mocked. The real
``TelegramAdapter`` inbound handler, the real ``SessionRouter``, the real
``PairingInterceptor``, the real SQLite ``PairingStore`` and the real
``MediaStore`` over a real temporary workspace. The operator approval in the
paired cases is the real one: the code the stranger was DM'd is fed back
through ``PairingStore.verify_and_consume`` exactly as ``arc gateway pair
approve`` does. Only the Telegram Bot API object is faked.
"""

from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from nacl.signing import SigningKey

from arcgateway.adapters.base import InboundDraft, PendingMedia
from arcgateway.media_store import MediaStore
from arcgateway.pairing import PairingStore, build_pairing_challenge
from arcgateway.parts import TextPart
from arcgateway.session import SessionRouter

pytestmark = pytest.mark.asyncio

_AGENT_DID = "did:arc:agent:media"
_OPERATOR_DID = "did:arc:org:operator/media-boundary"
_STRANGER_ID = 4242
_IMAGE_BYTES = b"\x89PNG\r\n\x1a\n" + b"ARC-PAIRING-BOUNDARY" + b"\x00" * 32

#: Anything the sender is told that would mean "a human must approve this
#: artefact before the agent sees it". The pairing DM is excluded by
#: construction — these cases run on a channel that is already paired.
_APPROVAL_WORDS = (
    "approve",
    "approval",
    "confirm",
    "permission",
    "authorise",
    "authorize",
    "allow this",
    "waiting for",
    "pending",
)

_PAIRING_WORDS = ("pair", "code")


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


@dataclass
class _Wire:
    """The fake Telegram Bot API: bytes it serves, replies it was asked to send."""

    data: bytes
    bot: Any = None

    def told_the_sender(self) -> str:
        texts: list[str] = []
        for call in getattr(self.bot.send_message, "await_args_list", []):
            texts.append(str(call.kwargs.get("text", "")))
            texts.extend(str(arg) for arg in call.args)
        return " ".join(texts)

    def downloads_attempted(self) -> int:
        """How many times the gateway went to the platform for bytes."""
        return int(self.bot.get_file.await_count)


@dataclass
class _Gateway:
    router: SessionRouter
    agent: _RecordingAgent
    store: PairingStore
    workspace: Path
    wire: _Wire
    operator_key: SigningKey
    adapter: Any = None

    def inbox_files(self) -> list[Path]:
        inbox = self.workspace / "inbox"
        if not inbox.exists():
            return []
        return sorted(path for path in inbox.rglob("*") if path.is_file())

    def parts(self) -> list[Any]:
        assert self.agent.received, "no envelope was delivered to the agent at all"
        return list(getattr(self.agent.received[-1], "parts", []))

    def media_parts(self) -> list[Any]:
        return [p for p in self.parts() if getattr(p, "kind", None) in ("image", "file", "audio")]


def _telegram_wire(data: bytes) -> _Wire:
    """A Bot whose get_file/download serves ``data`` and records send_message."""
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
    bot.send_photo = AsyncMock()
    bot.send_document = AsyncMock()
    return _Wire(data=data, bot=bot)


def _operator_trust_dir(tmp_path: Path) -> tuple[Path, SigningKey]:
    """A trust store holding one operator's Ed25519 pubkey.

    Approval is signature-required at every tier, so an operator approval that
    a test can make is an operator key the store can verify.
    """
    key = SigningKey.generate()
    trust_dir = tmp_path / "trust"
    trust_dir.mkdir(parents=True, exist_ok=True)
    operators = trust_dir / "operators.toml"
    pub_b64 = base64.b64encode(bytes(key.verify_key)).decode("ascii")
    operators.write_text(
        f'[operators."{_OPERATOR_DID}"]\npublic_key = "{pub_b64}"\n', encoding="utf-8"
    )
    operators.chmod(0o600)

    from arctrust import invalidate_cache

    invalidate_cache()
    return trust_dir, key


def _build_gateway(tmp_path: Path, *, data: bytes = _IMAGE_BYTES) -> _Gateway:
    """A real router + real pairing store + real media store, pairing ENFORCED."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    trust_dir, operator_key = _operator_trust_dir(tmp_path)

    agent = _RecordingAgent()
    store = PairingStore(db_path=tmp_path / "pairing.db", tier="personal", trust_dir=trust_dir)
    router = SessionRouter(
        executor=agent,
        pairing_store=store,
        media_store_for=lambda _did: MediaStore(workspace=workspace, max_bytes=8 * 1024 * 1024),
    )

    wire = _telegram_wire(data)
    gateway = _Gateway(
        router=router,
        agent=agent,
        store=store,
        workspace=workspace,
        wire=wire,
        operator_key=operator_key,
    )

    from arcgateway.adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter(
        bot_token="test-token-abc",
        # Empty static allowlist + require_pairing: the production shape for a
        # stranger. The adapter forwards them so the GATEWAY makes the call.
        allowed_user_ids=[],
        on_message=router.handle,
        agent_did=_AGENT_DID,
        require_pairing=True,
    )
    adapter._bot_id = 999
    application = MagicMock()
    application.bot = wire.bot
    adapter._application = application
    router.register_adapter(adapter)
    gateway.adapter = adapter
    return gateway


def _photo_update(*, caption: str | None = None) -> Any:
    """A Telegram photo Update as python-telegram-bot would deliver it."""
    update = MagicMock()
    update.update_id = 7
    message = update.effective_message
    message.text = None
    message.caption = caption
    message.document = None
    message.audio = None
    message.voice = None
    message.video = None

    size = MagicMock()
    size.file_id = "photo-file-id"
    size.file_unique_id = "photo-uid"
    size.file_size = len(_IMAGE_BYTES)
    size.width, size.height = 800, 600
    message.photo = [size]

    message.chat.id = _STRANGER_ID
    message.chat.type = "private"
    update.effective_chat.id = _STRANGER_ID
    update.effective_user.id = _STRANGER_ID
    update.effective_user.first_name = "Stranger"
    update.effective_user.username = "stranger"
    update.effective_user.is_bot = False
    return update


async def _deliver(gateway: _Gateway, update: Any) -> None:
    """Drive the adapter's real inbound handler, then let the router settle."""
    context = MagicMock()
    context.bot = gateway.wire.bot
    await gateway.adapter._handle_update(update, context=context)
    for _ in range(20):
        await asyncio.sleep(0.01)


async def _operator_approves(gateway: _Gateway) -> None:
    """Approve the pairing exactly as ``arc gateway pair approve`` does.

    The code is read back out of the DM the stranger was actually sent and
    cross-checked against the store's pending record, so the test approves the
    pairing the gateway really minted rather than one it fabricated behind the
    store's back. The approval carries a real Ed25519 operator signature,
    because approval is signature-required at every tier.
    """
    said = gateway.wire.told_the_sender()
    match = re.search(r"operator:\s*([A-Za-z0-9]{6,12})", said)
    assert match is not None, (
        f"no pairing code was DM'd to the unpaired sender, so there is nothing "
        f"for an operator to approve. The channel said: {said!r}"
    )

    pending = await gateway.store.list_pending()
    assert len(pending) == 1, f"expected exactly one pending pairing, got {pending}"
    code = pending[0]
    assert code.code == match.group(1), (
        "the code DM'd to the sender is not the code the store is holding"
    )

    challenge = build_pairing_challenge(code.code, code.minted_at)
    approved = await gateway.store.verify_and_consume(
        code.code,
        approver_did=_OPERATOR_DID,
        signature=bytes(gateway.operator_key.sign(challenge).signature),
    )
    assert approved is not None, "the minted pairing code was rejected by its own store"
    gateway.wire.bot.send_message.reset_mock()
    gateway.wire.bot.get_file.reset_mock()


def _said_anything_about_approval(text: str) -> list[str]:
    lowered = text.lower()
    return [word for word in _APPROVAL_WORDS if word in lowered]


# ---------------------------------------------------------------------------
# Unpaired: no run, no artefact, no download, nothing but the pairing flow
# ---------------------------------------------------------------------------


async def test_an_unpaired_senders_photo_starts_no_run(tmp_path: Path) -> None:
    """No pairing → no agent response, whatever is attached to the message."""
    gateway = _build_gateway(tmp_path)

    await _deliver(gateway, _photo_update(caption="urgent, please look"))

    assert gateway.agent.received == [], (
        "an unpaired sender's photo reached the agent — attaching a file is a "
        "way past the pairing gate"
    )


async def test_an_unpaired_senders_photo_is_never_written_to_the_workspace(
    tmp_path: Path,
) -> None:
    """An unapproved stranger cannot put bytes on the operator's disk."""
    gateway = _build_gateway(tmp_path)

    await _deliver(gateway, _photo_update())

    assert gateway.inbox_files() == [], (
        "an unpaired sender's artefact was written into the agent workspace — "
        "anyone who can find the bot can now store files on the operator's disk"
    )


async def test_an_unpaired_senders_artefact_is_never_even_downloaded(
    tmp_path: Path,
) -> None:
    """Custody runs after pairing: the bytes are not fetched at all.

    Stronger than "not stored". Fetching first and discarding later still
    spends the operator's bandwidth on a stranger's file and still brings
    untrusted bytes into the process.
    """
    gateway = _build_gateway(tmp_path)

    await _deliver(gateway, _photo_update())

    assert gateway.wire.downloads_attempted() == 0, (
        "the gateway downloaded an unpaired sender's artefact before deciding "
        "they were not allowed to talk to the agent"
    )


async def test_an_unpaired_sender_hears_only_the_pairing_flow(tmp_path: Path) -> None:
    """The only reply is the pairing code — not an agent turn, not an error."""
    gateway = _build_gateway(tmp_path)

    await _deliver(gateway, _photo_update(caption="look at this"))

    said = gateway.wire.told_the_sender()
    assert said.strip(), "the unpaired sender was told nothing at all"
    lowered = said.lower()
    assert any(word in lowered for word in _PAIRING_WORDS), (
        f"the unpaired sender was answered with something other than the pairing flow: {said!r}"
    )
    assert "look at this" not in lowered, (
        "the agent's surface echoed the unpaired sender's message back to them"
    )


# ---------------------------------------------------------------------------
# Paired: the channel is the boundary, and it is not re-asked per artefact
# ---------------------------------------------------------------------------


async def test_media_on_a_paired_channel_reaches_the_agent(tmp_path: Path) -> None:
    """The same photo, after one real operator approval, runs (REQ-305)."""
    gateway = _build_gateway(tmp_path)
    await _deliver(gateway, _photo_update())
    await _operator_approves(gateway)

    await _deliver(gateway, _photo_update(caption="the chart"))

    assert gateway.media_parts(), (
        "an approved channel still produced no media part — pairing approved "
        "the channel but the artefact on it went nowhere"
    )
    assert len(gateway.inbox_files()) == 1


async def test_a_paired_channel_raises_no_approval_prompt_for_an_artefact(
    tmp_path: Path,
) -> None:
    """The operator approved the channel; they are not asked again per file."""
    gateway = _build_gateway(tmp_path)
    await _deliver(gateway, _photo_update())
    await _operator_approves(gateway)

    await _deliver(gateway, _photo_update())

    said = gateway.wire.told_the_sender()
    offending = _said_anything_about_approval(said)
    assert not offending, (
        f"an artefact on an ALREADY-PAIRED channel raised a human approval "
        f"prompt ({offending}) — pairing is the authorization boundary and "
        f"re-asking per artefact is gate fatigue: {said!r}"
    )


async def test_every_artefact_on_a_paired_channel_runs_without_a_new_gate(
    tmp_path: Path,
) -> None:
    """Three photos in a row: three runs, three files, no escalation.

    A gate that fires on the second or the tenth artefact is the same defect
    as one that fires on the first — it is just harder to see.
    """
    gateway = _build_gateway(tmp_path)
    await _deliver(gateway, _photo_update())
    await _operator_approves(gateway)

    for index in range(3):
        await _deliver(gateway, _photo_update(caption=f"photo {index}"))

    assert len(gateway.agent.received) == 3, (
        f"three artefacts on a paired channel produced "
        f"{len(gateway.agent.received)} runs — one of them was held back"
    )
    assert len(gateway.inbox_files()) == 3
    offending = _said_anything_about_approval(gateway.wire.told_the_sender())
    assert not offending, f"a later artefact raised an approval prompt ({offending})"


async def test_inbound_media_never_reaches_the_agents_human_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-tool-call gate is not a per-artefact gate.

    ``HumanGate.request`` is the one human-approval gate in Arc, reached from
    ``arcagent.core.tool_registry`` when a tool call composes a forbidden
    trifecta. Taking custody of an inbound artefact is not a tool call, and
    must never route through it. Patching the real method is the assertion:
    if any part of the media path ever calls it, this test dies where it happens.
    """
    from arcagent.tools.human_gate import HumanGate

    async def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "the media path asked HumanGate to approve an inbound artefact — "
            "the paired channel is already the authorization boundary (REQ-305)"
        )

    monkeypatch.setattr(HumanGate, "request", _forbidden)

    gateway = _build_gateway(tmp_path)
    await _deliver(gateway, _photo_update())
    await _operator_approves(gateway)

    await _deliver(gateway, _photo_update())

    assert gateway.media_parts(), "the artefact did not arrive, so nothing was proven"


# ---------------------------------------------------------------------------
# Operator-authenticated: the token IS the approval
# ---------------------------------------------------------------------------


async def test_media_on_the_operator_console_needs_no_second_approval(
    tmp_path: Path,
) -> None:
    """REQ-305's second half: an operator-authenticated channel is authorised.

    ``web`` is the operator console — every ``/ws/chat`` connection is already
    gated by arcui's operator token before it reaches the router. A second
    approval on top of the token, per artefact, locks the operator out of
    their own dashboard.
    """
    gateway = _build_gateway(tmp_path)

    async def _fetch(_limit_bytes: int) -> bytes:
        return _IMAGE_BYTES

    draft = InboundDraft(
        platform="web",
        chat_id="console",
        user_did="did:arc:web:operator",
        agent_did=_AGENT_DID,
        parts=[
            TextPart(text="here is the chart"),
            PendingMedia(
                kind="image",
                mime="image/png",
                declared_name="chart.png",
                fetch=_fetch,
            ),
        ],
    )
    await gateway.router.handle(draft)
    for _ in range(20):
        await asyncio.sleep(0.01)

    assert gateway.media_parts(), (
        "media on the token-authenticated operator console was gated — the "
        "operator was locked out of their own dashboard"
    )
    assert len(gateway.inbox_files()) == 1
    assert not _said_anything_about_approval(gateway.wire.told_the_sender())
