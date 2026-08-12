"""The contract every platform adapter must satisfy (SPEC-065 T-923, COMP-013).

Parametrised over the adapters the gateway can actually discover, so adding a
platform adds its coverage and deleting one removes it, with no edit here.

This suite exists because of the defect it is written to catch. The Telegram
adapter registered ``MessageHandler(filters.TEXT, ...)``, so a photo never
reached a handler and no run ever started — and every per-layer unit test in
the repo passed the whole time, because each layer was individually fine. Only
a case that drives a real platform payload through a real handler fails on a
one-line handler registration.

The inbound-image case is therefore expected to FAIL until the media path
lands. That failure is the bug, reproduced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcgateway.adapters.registry import discover_adapters


@dataclass(frozen=True)
class AdapterUnderTest:
    """One discovered platform and the hooks the contract needs to exercise it."""

    name: str
    build: Any
    """() -> adapter, wired to an AsyncMock on_message."""
    text_payload: Any
    """() -> a platform payload carrying plain text."""
    image_payload: Any
    """() -> a platform payload carrying a photo."""
    deliver: Any
    """async (adapter, payload) -> None — drive the adapter's real inbound path."""


# --- Telegram -----------------------------------------------------------------


def _telegram_available() -> bool:
    """True when the registry actually discovered telegram (T-937/T-940).

    Gated on the registry rather than on a distribution being importable. The
    old gate asked ``find_spec("arcgateway_telegram")``, which went permanently
    False the moment T-940 folded the distribution in-tree — so the suite
    SKIPPED, and a skip reads as "no failure". This case is the photo defect's
    canary; it has to be able to fail.
    """
    return "telegram" in {spec.name for spec in discover_adapters()}


def _telegram_adapter() -> Any:
    from arcgateway.adapters.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        bot_token="test-token-abc",
        allowed_user_ids=[42],
        on_message=AsyncMock(),
        agent_did="did:arc:agent:test",
    )
    adapter._bot_id = 999
    return adapter


def _telegram_update(*, text: str | None, photo: bool) -> Any:
    update = MagicMock()
    update.update_id = 1
    update.effective_message = MagicMock()
    update.effective_message.text = text
    update.effective_message.caption = None
    update.effective_message.document = None
    if photo:
        size = MagicMock()
        size.file_id = "photo-file-id"
        size.file_size = 2048
        update.effective_message.photo = [size]
    else:
        update.effective_message.photo = []
    update.effective_message.chat = MagicMock()
    update.effective_message.chat.id = 42
    update.effective_user = MagicMock()
    update.effective_user.id = 42
    update.effective_user.first_name = "Op"
    update.effective_user.username = "op"
    return update


async def _telegram_deliver(adapter: Any, payload: Any) -> None:
    await adapter._handle_update(payload, context=MagicMock())


_CANDIDATES = [
    AdapterUnderTest(
        name="telegram",
        build=_telegram_adapter,
        text_payload=lambda: _telegram_update(text="hello", photo=False),
        image_payload=lambda: _telegram_update(text=None, photo=True),
        deliver=_telegram_deliver,
        # gate: only collected when the registry discovers it
    )
]


def _discovered() -> list[AdapterUnderTest]:
    """Adapters this checkout can actually exercise.

    Once adapters move in-tree (T-937/T-940) this becomes a scan of the
    registry rather than a literal list, and every platform inherits the suite.
    """
    out = []
    for candidate in _CANDIDATES:
        if candidate.name == "telegram" and not _telegram_available():
            continue
        out.append(candidate)
    return out


def _ids(adapters: list[AdapterUnderTest]) -> list[str]:
    return [a.name for a in adapters]


_ADAPTERS = _discovered()
pytestmark = pytest.mark.skipif(not _ADAPTERS, reason="no platform adapter available")


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_spec", _ADAPTERS, ids=_ids(_ADAPTERS))
async def test_inbound_text_reaches_the_gateway(adapter_spec: AdapterUnderTest) -> None:
    """A text message produces exactly one inbound event. The path that works today."""
    adapter = adapter_spec.build()

    await adapter_spec.deliver(adapter, adapter_spec.text_payload())

    assert adapter._on_message.await_count == 1, (
        f"{adapter_spec.name}: a text message produced no inbound event"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_spec", _ADAPTERS, ids=_ids(_ADAPTERS))
async def test_inbound_image_reaches_the_gateway(adapter_spec: AdapterUnderTest) -> None:
    """A photo produces an inbound event carrying a media part.

    SPEC-065 REQ-296/297. Red until the media path lands: a text-only handler
    registration means the photo never arrives, which is the reported defect.
    """
    adapter = adapter_spec.build()

    await adapter_spec.deliver(adapter, adapter_spec.image_payload())

    assert adapter._on_message.await_count == 1, (
        f"{adapter_spec.name}: a photo produced NO inbound event at all — "
        "the handler never saw it"
    )
    event = adapter._on_message.await_args.args[0]
    parts = getattr(event, "parts", None)
    assert parts, f"{adapter_spec.name}: inbound event carries no parts list"
    assert any(getattr(p, "kind", "") == "image" for p in parts), (
        f"{adapter_spec.name}: a photo produced no image part"
    )
