"""DeliveryTarget — string-addressable routing for outbound messages.

Format: ``platform:chat_id`` or ``platform:chat_id:thread_id``

Examples::

    DeliveryTarget.parse("telegram:12345")
    DeliveryTarget.parse("telegram:12345:67890")
    DeliveryTarget.parse("slack:C123ABC")
    DeliveryTarget.parse("slack:C123ABC:T9876")

The colon-delimited format is intentionally simple. It must survive TOML config
round-trips (``deliver_to = "telegram:joshs-channel"``) and CLI arguments
without quoting.

Design note: thread_id is optional — not all platforms support threading.
Adapters that receive a non-None thread_id but don't support threading
SHOULD fall back to chat_id delivery and log a warning rather than erroring.

T1.13 addition (SPEC-018 §3.4) / SPEC-017 R-040 migration:
    ``DeliverySenderImpl`` is a platform-adapter router. It was
    originally consumed by ``arcagent.modules.scheduler`` (deleted by
    SPEC-017 R-040) via a DeliverySender Protocol. With the new
    ``arcagent.modules.proactive`` engine, callers wrap a
    ``DeliverySenderImpl`` instance inside their schedule ``handler``
    callable. The class itself is unchanged — only its consumer moved.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, field_validator

if TYPE_CHECKING:
    from arcgateway.adapters.base import BasePlatformAdapter

_logger = logging.getLogger("arcgateway.delivery")

# Supported platform identifiers. Adapters register here in T1.7.
# Kept loose (str) so stubs work without importing platform SDKs.
_KNOWN_PLATFORMS: frozenset[str] = frozenset(
    {"telegram", "slack", "discord", "whatsapp", "signal", "matrix", "email"}
)


class DeliveryTarget(BaseModel):
    """Parsed destination for an outbound message.

    Attributes:
        platform: Normalised platform name (e.g. "telegram", "slack").
        chat_id: Platform-specific channel/conversation/user identifier.
        thread_id: Optional thread within the chat (Telegram message thread,
            Slack thread_ts, Discord thread). None if not applicable.
    """

    platform: str
    chat_id: str
    thread_id: str | None = None

    @field_validator("platform")
    @classmethod
    def _normalise_platform(cls, v: str) -> str:
        """Lowercase and strip whitespace; warn on unknown platform."""
        normalised = v.strip().lower()
        if normalised not in _KNOWN_PLATFORMS:
            _logger.warning(
                "Unknown platform %r — delivery will fail unless a matching adapter "
                "is registered at runtime. Known platforms: %s",
                normalised,
                ", ".join(sorted(_KNOWN_PLATFORMS)),
            )
        return normalised

    @field_validator("chat_id")
    @classmethod
    def _require_nonempty_chat_id(cls, v: str) -> str:
        """Reject empty chat_id — every platform requires an explicit target."""
        stripped = v.strip()
        if not stripped:
            msg = "chat_id must not be empty"
            raise ValueError(msg)
        return stripped

    @classmethod
    def parse(cls, s: str) -> DeliveryTarget:
        """Parse a colon-delimited delivery address string.

        Supported formats:
        - ``platform:chat_id``
        - ``platform:chat_id:thread_id``

        Args:
            s: Address string, e.g. "telegram:12345:67890".

        Returns:
            Parsed DeliveryTarget.

        Raises:
            ValueError: If the string cannot be parsed.
        """
        parts = s.split(":", maxsplit=2)
        if len(parts) < 2:
            msg = (
                f"Invalid DeliveryTarget format {s!r}. "
                "Expected 'platform:chat_id' or 'platform:chat_id:thread_id'."
            )
            raise ValueError(msg)

        platform = parts[0]
        chat_id = parts[1]
        thread_id = parts[2] if len(parts) == 3 and parts[2] else None

        return cls(platform=platform, chat_id=chat_id, thread_id=thread_id)

    def __str__(self) -> str:
        """Serialize back to canonical colon-delimited form."""
        if self.thread_id is not None:
            return f"{self.platform}:{self.chat_id}:{self.thread_id}"
        return f"{self.platform}:{self.chat_id}"


class DeliverySenderImpl:
    """arcgateway implementation of the scheduler's DeliverySender Protocol.

    T1.13 (SPEC-018 §3.4 Platform Delivery).

    Satisfies ``arcagent.modules.scheduler.delivery.DeliverySender`` without
    arcagent importing arcgateway (dependency direction preserved per SDD §5).

    The scheduler holds a reference to this object injected by GatewayRunner
    at startup.  Each ``send()`` call:
      1. Parses the raw address string into a ``DeliveryTarget``.
      2. Looks up the registered adapter for ``target.platform``.
      3. Calls ``adapter.send(target, message)``.

    Adapters are registered via ``register_adapter()`` at gateway startup.
    An unknown platform raises ``ValueError`` rather than silently dropping
    the message — caller (CronRunner) catches and logs.

    Thread safety: ``_adapters`` is mutated only during startup before any
    cron jobs can fire; no lock needed for the read path.
    """

    def __init__(self) -> None:
        # platform name → adapter instance
        self._adapters: dict[str, BasePlatformAdapter] = {}

    def register_adapter(self, platform: str, adapter: BasePlatformAdapter) -> None:
        """Register a platform adapter for outbound delivery.

        Args:
            platform: Lowercase platform name (e.g. "telegram", "slack").
            adapter: Adapter instance satisfying BasePlatformAdapter Protocol.
        """
        self._adapters[platform.lower()] = adapter
        _logger.debug("DeliverySenderImpl: registered adapter for platform %r", platform)

    async def send(self, target: Any, message: str) -> None:
        """Deliver message to the platform identified by target.

        Accepts a raw address string (str) OR a DeliveryTarget object.
        Parses strings on the fly so the scheduler can pass the raw config
        value without constructing a DeliveryTarget itself.

        Args:
            target: ``DeliveryTarget`` instance, or a colon-delimited address
                string (e.g. "telegram:12345").
            message: The text to deliver.

        Raises:
            ValueError: If target cannot be parsed or platform has no adapter.
        """
        delivery_target = self._resolve_target(target)
        adapter = self._adapters.get(delivery_target.platform)

        if adapter is None:
            msg = (
                f"No adapter registered for platform {delivery_target.platform!r}. "
                f"Registered platforms: {sorted(self._adapters)}"
            )
            raise ValueError(msg)

        _logger.debug(
            "DeliverySenderImpl.send: platform=%s chat_id=%s",
            delivery_target.platform,
            delivery_target.chat_id,
        )
        from arcgateway.audit import emit_event as _arc_emit

        try:
            await adapter.send(delivery_target, message)
            _arc_emit(
                action="gateway.delivery.sent",
                target=str(delivery_target),
                outcome="allow",
                extra={"platform": delivery_target.platform, "chat_id": delivery_target.chat_id},
            )
        except Exception:
            _arc_emit(
                action="gateway.delivery.failed",
                target=str(delivery_target),
                outcome="error",
                extra={"platform": delivery_target.platform, "chat_id": delivery_target.chat_id},
            )
            raise

    @staticmethod
    def _resolve_target(target: Any) -> DeliveryTarget:
        """Coerce target to DeliveryTarget.

        Accepts DeliveryTarget instances directly or raw address strings.
        """
        if isinstance(target, DeliveryTarget):
            return target
        if isinstance(target, str):
            return DeliveryTarget.parse(target)
        msg = f"Cannot resolve delivery target of type {type(target).__name__}"
        raise TypeError(msg)
