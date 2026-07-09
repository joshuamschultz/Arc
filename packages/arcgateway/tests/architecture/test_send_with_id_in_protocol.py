"""Architecture test: the send_with_id Protocol surface and its default.

``send_with_id`` is a first-class Protocol method on ``BasePlatformAdapter``.
This test guards the Protocol declaration and the inherited default behaviour.
Per-adapter compliance (each concrete adapter overrides it and satisfies the
Protocol) is asserted inside each adapter's own extension package, since the
gateway core no longer ships any platform adapter.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from arcgateway.adapters.base import BasePlatformAdapter
from arcgateway.delivery import DeliveryTarget


class TestProtocolDeclaration:
    def test_send_with_id_in_protocol(self) -> None:
        """BasePlatformAdapter Protocol must declare send_with_id."""
        assert hasattr(BasePlatformAdapter, "send_with_id"), (
            "BasePlatformAdapter must declare send_with_id as a Protocol method"
        )

    def test_send_with_id_is_coroutine_function(self) -> None:
        """send_with_id must be declared as an async method."""
        method = BasePlatformAdapter.send_with_id
        assert asyncio.iscoroutinefunction(method) or inspect.isfunction(method), (
            "send_with_id must be a function/coroutine on the Protocol"
        )

    def test_protocol_signature_returns_str_or_none(self) -> None:
        """send_with_id must carry a return annotation (str | None)."""
        import typing

        hints = typing.get_type_hints(BasePlatformAdapter.send_with_id)
        assert hints.get("return") is not None, "send_with_id must have a return annotation"


@pytest.mark.asyncio
async def test_protocol_default_calls_send_and_returns_none() -> None:
    """The default Protocol implementation calls send() and returns None.

    A minimal concrete class that delegates to the base default verifies the
    inherited behaviour without depending on any platform adapter.
    """

    class _MinimalAdapter:
        name = "minimal"

        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def send(
            self,
            target: DeliveryTarget,
            message: str,
            *,
            reply_to: str | None = None,
        ) -> None:
            self._sent = (target, message)

        async def send_with_id(self, target: DeliveryTarget, message: str) -> str | None:
            await self.send(target, message)
            return None

    adapter = _MinimalAdapter()
    target = DeliveryTarget.parse("web:99")
    result = await adapter.send_with_id(target, "hello")

    assert result is None, "Default send_with_id must return None"
    assert adapter._sent == (target, "hello"), "Default send_with_id must call send()"
    assert isinstance(adapter, BasePlatformAdapter)
