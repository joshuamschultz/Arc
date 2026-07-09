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
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, field_validator

_logger = logging.getLogger("arcgateway.delivery")

# Supported platform identifiers. Adapters register here in T1.7.
# Kept loose (str) so stubs work without importing platform SDKs.
# "python" covers the in-process PythonAdapter (FastAPI hosts, CLI demos)
# that drive the gateway programmatically rather than over a chat wire.
_KNOWN_PLATFORMS: frozenset[str] = frozenset(
    {
        "telegram",
        "slack",
        "discord",
        "whatsapp",
        "signal",
        "matrix",
        "email",
        "web",
        "python",
    }
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
