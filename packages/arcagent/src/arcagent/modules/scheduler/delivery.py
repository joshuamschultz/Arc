"""DeliverySender Protocol — arcagent-owned contract for outbound delivery.

arcagent defines the Protocol; arcgateway provides the implementation
(``arcgateway.delivery.DeliverySenderImpl``).  This keeps the dependency
direction correct: arcagent never imports arcgateway.

Usage in tests::

    from unittest.mock import AsyncMock
    from arcagent.modules.scheduler.delivery import DeliverySender

    sender = AsyncMock(spec=DeliverySender)
    assert isinstance(sender, DeliverySender)  # runtime_checkable

T1.13 (SPEC-018 §3.4 Platform Delivery).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DeliverySender(Protocol):
    """Outbound delivery interface.

    Implementors (e.g. ``arcgateway.delivery.DeliverySenderImpl``) route
    the message to the appropriate platform adapter based on *target*.

    Args:
        target: A ``DeliveryTarget`` object **or** a raw colon-delimited
            address string such as ``"telegram:12345"`` or
            ``"slack:C123:T9876"``.  Implementations are expected to
            accept both forms.
        message: The text to deliver.
    """

    async def send(self, target: Any, message: str) -> None:
        """Deliver *message* to *target*.

        Raises:
            ValueError: If the target cannot be resolved or the platform
                has no registered adapter.
            TypeError: If *target* is not a recognised type.
        """
        ...
