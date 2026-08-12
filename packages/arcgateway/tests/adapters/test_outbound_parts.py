"""T-941 — an agent reply carrying a file is delivered, or described, never lost.

SPEC-065 REQ-311, COMP-004/COMP-005. Outbound uses the same part vocabulary as
inbound: ``send(target, parts)``. Today ``BasePlatformAdapter.send`` takes a
``message: str``, so an agent that produces a report has no way to hand it to a
channel at all.

The half of REQ-311 that is easy to get wrong is the degradation clause. "IF
the platform cannot carry that kind or size THEN the adapter SHALL degrade to a
text description rather than losing the turn" is a delivery guarantee, not an
exception-handling style. A ``try/except: pass`` around ``send_document``
satisfies "no exception raised" and fails the requirement completely — the
operator asked for a report and got silence. So every degradation case here
asserts that **something reached the channel** and that it **names the file**;
none of them assert only that no exception escaped.

Two ways a platform can be unable to carry an artefact, and both are tested:

* declared — the platform's ``supports`` does not include the kind, which the
  gateway can know before it tries; and
* discovered — the wire refuses it (Telegram rejects documents over its own
  limit), which nothing can know in advance.

Assumed of the green implementation:

    adapter.send(target, parts)  where parts is a list of TextPart/MediaPart
    registry.AdapterSpec(...).supports declares carriable kinds
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcgateway.adapters import registry
from arcgateway.delivery import DeliveryTarget
from arcgateway.parts import MediaPart, TextPart

pytestmark = pytest.mark.asyncio

_AGENT_DID = "did:arc:agent:media"
_REPORT = b"%PDF-1.7\nquarterly numbers\n%%EOF\n"


def _target() -> DeliveryTarget:
    return DeliveryTarget(platform="telegram", chat_id="42")


def _stored_report(tmp_path: Path, name: str = "q3-report.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(_REPORT)
    return path


def _telegram_adapter(bot: Any) -> Any:
    """The real TelegramAdapter over a fake Bot."""
    import importlib

    for module_path in (
        "arcgateway.adapters.telegram",
        "arcgateway.adapters.telegram.adapter",
        "arcgateway_telegram.adapter",
    ):
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            continue
        adapter_cls = getattr(module, "TelegramAdapter", None)
        if adapter_cls is not None:
            break
    else:  # pragma: no cover - reported by the failing assertion instead
        pytest.fail("no TelegramAdapter importable")

    adapter = adapter_cls(
        bot_token="test-token-abc",
        allowed_user_ids=[42],
        on_message=AsyncMock(),
        agent_did=_AGENT_DID,
    )
    adapter._bot_id = 999
    application = MagicMock()
    application.bot = bot
    adapter._application = application
    return adapter


def _bot(*, document_fails: bool = False, audio_fails: bool = False) -> Any:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    bot.send_photo = AsyncMock(return_value=MagicMock(message_id=2))
    bot.send_audio = AsyncMock(
        side_effect=RuntimeError("Bad Request: file is too big") if audio_fails else None
    )
    bot.send_document = AsyncMock(
        side_effect=RuntimeError("Bad Request: file is too big")
        if document_fails
        else MagicMock(message_id=3)
    )
    bot.send_chat_action = AsyncMock()
    return bot


#: Markers that a parts list was stringified and handed to the platform whole.
#: Without this guard every degradation assertion below passes vacuously against
#: today's ``send(target, message: str)``: the repr of a MediaPart contains the
#: filename, so "the fallback names the file" is satisfied by an operator
#: receiving ``[MediaPart(kind='file', ...)]`` as their message.
_REPR_LEAK = ("MediaPart(", "TextPart(", "kind=", "ref=", "declared_name=")


def _everything_said(bot: Any) -> str:
    """Every piece of text the platform was asked to deliver, raw."""
    said: list[str] = []
    for call in bot.send_message.await_args_list:
        said.append(str(call.kwargs.get("text", "")))
        said.extend(str(arg) for arg in call.args)
    return " ".join(said)


def _delivered_text(bot: Any) -> str:
    """What a human on the channel would actually read."""
    said = _everything_said(bot)
    leaked = [marker for marker in _REPR_LEAK if marker in said]
    assert not leaked, (
        f"the parts list was stringified and sent as the message ({leaked}): "
        f"{said!r} — send() still takes a text blob, so a structured reply "
        "reaches the operator as a Python repr"
    )
    return said


# --- The happy path ----------------------------------------------------------


async def test_a_reply_carrying_a_file_is_delivered_as_a_file(tmp_path: Path) -> None:
    """REQ-311: the produced report reaches the channel the operator asked from."""
    bot = _bot()
    adapter = _telegram_adapter(bot)
    report = _stored_report(tmp_path)

    await adapter.send(
        _target(),
        [
            TextPart(text="here is the quarterly report"),
            MediaPart(
                kind="file",
                mime="application/pdf",
                declared_name="q3-report.pdf",
                ref=str(report),
            ),
        ],
    )

    assert bot.send_document.await_count == 1, (
        "the file part was not delivered as a document — an agent still cannot "
        "send a file back"
    )
    assert "quarterly report" in _delivered_text(bot), "the text part was dropped"


async def test_an_image_part_is_delivered_as_a_photo(tmp_path: Path) -> None:
    """Kind is carried through, not flattened to "some attachment"."""
    bot = _bot()
    adapter = _telegram_adapter(bot)
    chart = tmp_path / "chart.png"
    chart.write_bytes(b"\x89PNG\r\n\x1a\nchart")

    await adapter.send(
        _target(),
        [
            MediaPart(
                kind="image",
                mime="image/png",
                declared_name="chart.png",
                ref=str(chart),
            )
        ],
    )

    assert bot.send_photo.await_count == 1, "an image part was not sent as a photo"


async def test_a_text_only_reply_still_works(tmp_path: Path) -> None:
    """REQ-315: the outbound path that works today keeps working."""
    bot = _bot()
    adapter = _telegram_adapter(bot)

    await adapter.send(_target(), [TextPart(text="done")])

    assert _delivered_text(bot).strip() == "done", (
        "a single text part did not arrive as its own words"
    )
    assert bot.send_document.await_count == 0


# --- Degradation: the turn survives ------------------------------------------


async def test_a_kind_the_platform_does_not_declare_degrades_to_a_description(
    tmp_path: Path,
) -> None:
    """Declared incapability: the gateway knows before it tries (REQ-310/311)."""
    spec = next(
        (s for s in registry.discover_adapters() if s.name == "telegram"), None
    )
    assert spec is not None, "telegram is not in the discovered roster"

    unsupported = next(
        (kind for kind in ("audio", "file", "image") if kind not in set(spec.supports)),
        None,
    )
    if unsupported is None:
        pytest.skip("telegram declares every kind; nothing to degrade")

    bot = _bot()
    adapter = _telegram_adapter(bot)
    artefact = tmp_path / f"clip.{unsupported}"
    artefact.write_bytes(_REPORT)

    await adapter.send(
        _target(),
        [
            MediaPart(
                kind=unsupported,  # type: ignore[arg-type]  # reason: driving the undeclared kind is the point
                mime="application/octet-stream",
                declared_name=artefact.name,
                ref=str(artefact),
            )
        ],
    )

    said = _delivered_text(bot)
    assert said.strip(), (
        f"a {unsupported!r} part the platform does not declare produced no "
        "delivery at all — the turn was lost"
    )
    assert artefact.name in said, (
        f"the degradation text does not name the file: {said!r}"
    )


async def test_a_file_the_wire_refuses_degrades_to_a_description(
    tmp_path: Path,
) -> None:
    """Discovered incapability: the platform rejects the upload mid-send.

    Telegram answers an oversized document with a Bad Request. Nothing can know
    that in advance, so the turn is protected here or nowhere.
    """
    bot = _bot(document_fails=True)
    adapter = _telegram_adapter(bot)
    report = _stored_report(tmp_path, "enormous-export.pdf")

    await adapter.send(
        _target(),
        [
            MediaPart(
                kind="file",
                mime="application/pdf",
                declared_name="enormous-export.pdf",
                ref=str(report),
            )
        ],
    )

    said = _delivered_text(bot)
    assert said.strip(), (
        "the platform refused the upload and nothing was delivered — the turn "
        "was lost to a swallowed exception"
    )
    assert "enormous-export.pdf" in said, (
        f"the fallback does not name the file the operator asked for: {said!r}"
    )


async def test_the_text_of_a_reply_survives_a_failed_attachment(
    tmp_path: Path,
) -> None:
    """The words are the part of the turn most worth saving."""
    bot = _bot(document_fails=True)
    adapter = _telegram_adapter(bot)
    report = _stored_report(tmp_path)

    await adapter.send(
        _target(),
        [
            TextPart(text="the numbers are in the attached report"),
            MediaPart(
                kind="file",
                mime="application/pdf",
                declared_name="q3-report.pdf",
                ref=str(report),
            ),
        ],
    )

    assert "the numbers are in the attached report" in _delivered_text(bot), (
        "a failed attachment took the reply's text down with it"
    )


async def test_a_missing_artefact_does_not_lose_the_turn(tmp_path: Path) -> None:
    """The reference outlives the file: still a delivery, still no exception."""
    bot = _bot()
    adapter = _telegram_adapter(bot)

    await adapter.send(
        _target(),
        [
            TextPart(text="report attached"),
            MediaPart(
                kind="file",
                mime="application/pdf",
                declared_name="gone.pdf",
                ref=str(tmp_path / "gone.pdf"),
            ),
        ],
    )

    assert "report attached" in _delivered_text(bot), (
        "a media reference pointing at nothing lost the whole reply"
    )
