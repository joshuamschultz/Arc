"""Every way custody can fail, and the promise that holds across all of them.

SPEC-065 review: three of these four branches dropped the artefact out of the
message and told the sender nothing. A media-only message then vanished
entirely — no turn, no reply — which from the sender's side is indistinguishable
from an agent that ignored them, and is the REQ-315 regression the spec's own
operator constraints forbid.

The invariant every case below asserts: **the message survives and the sender
is answered.** The bytes may be refused; the fact that something was sent never
is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcgateway.adapters.base import InboundDraft, MediaTooLargeOnWireError, PendingMedia
from arcgateway.executor import InboundEvent
from arcgateway.media_custody import MediaCustodian
from arcgateway.media_store import MediaStore
from arcgateway.parts import MediaPart, TextPart

_AGENT = "did:arc:alpha"
_USER = "did:arc:user"


class _Replies:
    """Collects what the sender was told."""

    def __init__(self) -> None:
        self.said: list[str] = []

    async def __call__(self, _event: InboundEvent, text: str) -> None:
        self.said.append(text)


def _pending(
    *, fetch: Any = None, size_bytes: int | None = None, name: str = "chart.png"
) -> PendingMedia:
    async def _ok(_limit: int) -> bytes:
        return b"PNGBYTES"

    return PendingMedia(
        kind="image",
        mime="image/png",
        declared_name=name,
        fetch=fetch or _ok,
        size_bytes=size_bytes,
    )


def _draft(*parts: Any) -> InboundDraft:
    return InboundDraft(
        platform="telegram",
        chat_id="9001",
        user_did=_USER,
        agent_did=_AGENT,
        parts=parts,
    )


def _event() -> InboundEvent:
    return InboundEvent(
        platform="telegram",
        chat_id="9001",
        user_did=_USER,
        agent_did=_AGENT,
        message="here is the chart",
        parts=[TextPart(text="here is the chart")],
        session_key="telegram:9001",
    )


def _custodian(store: MediaStore | None, replies: _Replies) -> MediaCustodian:
    return MediaCustodian(media_store_for=lambda _did: store, send_reply=replies)


# --- the artefact is kept ---------------------------------------------------


async def test_a_stored_artefact_becomes_a_workspace_reference(tmp_path: Path) -> None:
    replies = _Replies()
    store = MediaStore(workspace=tmp_path / "ws", max_bytes=1024)

    taken = await _custodian(store, replies).take(
        _draft(TextPart(text="here is the chart"), _pending()), _event()
    )

    assert taken is not None
    media = [p for p in taken.parts if isinstance(p, MediaPart)]
    assert len(media) == 1
    assert not Path(media[0].ref).is_absolute(), "the ref leaked an absolute host path"
    assert replies.said == [], "a successful store should not report anything"


# --- the artefact is refused, and the message still arrives -----------------


async def test_an_agent_with_no_workspace_still_delivers_the_message(
    tmp_path: Path,
) -> None:
    """The docstring promised the artefact is 'named rather than stored'."""
    replies = _Replies()

    taken = await _custodian(None, replies).take(
        _draft(TextPart(text="here is the chart"), _pending()), _event()
    )

    assert taken is not None, "a message was dropped because its artefact could not be stored"
    assert "chart.png" in taken.message
    assert replies.said, "the sender attached a file and was told nothing"
    assert "workspace" in replies.said[0]


async def test_a_media_only_message_survives_a_refused_artefact() -> None:
    """The case that used to disappear completely: no text to fall back on."""
    replies = _Replies()

    taken = await _custodian(None, replies).take(_draft(_pending()), _event())

    assert taken is not None, "a photo-only message vanished with no turn and no reply"
    assert "chart.png" in taken.message
    assert replies.said


async def test_an_artefact_declared_oversize_is_refused_before_it_is_fetched(
    tmp_path: Path,
) -> None:
    """The declared size may refuse early — it may never accept."""
    fetched = False

    async def _fetch(_limit: int) -> bytes:
        nonlocal fetched
        fetched = True
        return b"x"

    replies = _Replies()
    store = MediaStore(workspace=tmp_path / "ws", max_bytes=10)

    taken = await _custodian(store, replies).take(
        _draft(_pending(fetch=_fetch, size_bytes=5_000_000)), _event()
    )

    assert not fetched, "the gateway paid for bytes it had already been told to refuse"
    assert taken is not None
    assert any("too large" in said for said in replies.said)


async def test_an_artefact_that_passes_the_ceiling_mid_download_is_reported_as_size(
    tmp_path: Path,
) -> None:
    """Aborted-for-size must not be reported as 'could not be downloaded'."""

    async def _fetch(limit: int) -> bytes:
        raise MediaTooLargeOnWireError(limit)

    replies = _Replies()
    store = MediaStore(workspace=tmp_path / "ws", max_bytes=10)

    await _custodian(store, replies).take(_draft(_pending(fetch=_fetch)), _event())

    assert any("too large" in said for said in replies.said), replies.said


async def test_a_download_failure_is_reported_on_the_channel(tmp_path: Path) -> None:
    async def _fetch(_limit: int) -> bytes:
        raise RuntimeError("the platform hung up")

    replies = _Replies()
    store = MediaStore(workspace=tmp_path / "ws", max_bytes=1024)

    taken = await _custodian(store, replies).take(
        _draft(TextPart(text="here"), _pending(fetch=_fetch)), _event()
    )

    assert taken is not None
    assert any("could not be downloaded" in said for said in replies.said)


async def test_an_artefact_over_the_ceiling_by_its_real_size_is_refused(
    tmp_path: Path,
) -> None:
    """The bytes that arrive are measured even when nothing was declared."""

    async def _fetch(_limit: int) -> bytes:
        return b"A" * 4096

    replies = _Replies()
    store = MediaStore(workspace=tmp_path / "ws", max_bytes=8)

    await _custodian(store, replies).take(_draft(_pending(fetch=_fetch)), _event())

    assert any("too large" in said for said in replies.said), replies.said
    assert not list((tmp_path / "ws").rglob("*.png")), "a refused artefact reached the disk"


async def test_a_draft_with_no_parts_at_all_produces_no_turn() -> None:
    """The one case where returning None is right: there is nothing to run."""
    replies = _Replies()
    assert await _custodian(None, replies).take(_draft(), _event()) is None
