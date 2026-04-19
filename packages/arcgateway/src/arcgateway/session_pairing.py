"""PairingInterceptor — pairing gate before session routing.

Extracts all pairing-check and DM-delivery logic from SessionRouter so that
the router core (race-guard + task spawn) remains free of pairing concerns.

Design (SDD §3.1 DM Pairing / T1.8):
    - Checks whether a user is in the approved allowlist.
    - When not approved: mints a pairing code and DMs the user via the adapter.
    - Wires up to 5 closed TODO(M1 T1.7 integration) items by receiving the
      adapter_map at construction time and calling adapter.send() directly.
    - No-op (all users approved) when user_allowlist is None.
    - Duck-typed for testability: pairing_store only needs .mint_code() coroutine.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcgateway.telemetry import hash_user_did

if TYPE_CHECKING:
    from arcgateway.executor import InboundEvent

_logger = logging.getLogger("arcgateway.session_pairing")


class PairingInterceptor:
    """Intercepts messages from unapproved users and issues DM pairing codes.

    Duck-typed against PairingStore: any object with a ``mint_code(platform,
    platform_user_id)`` coroutine works here (enabling test doubles without
    subclassing PairingStore).

    Attributes:
        _user_allowlist: Set of approved user_did values.  None = all approved.
        _pairing_store:  Optional store with mint_code() coroutine.
        _adapter_map:    Maps platform name → adapter for DM delivery.
    """

    def __init__(
        self,
        *,
        user_allowlist: set[str] | None = None,
        pairing_store: object | None = None,
        pairing_db_path: Path | None = None,
        adapter_map: dict[str, Any] | None = None,
    ) -> None:
        """Initialise PairingInterceptor.

        Args:
            user_allowlist:  Approved user DID set.  None disables enforcement.
            pairing_store:   Any object with a ``mint_code()`` coroutine.  When
                             None and pairing_db_path is given, a PairingStore is
                             created at that path.
            pairing_db_path: Convenience path for auto-creating a PairingStore.
            adapter_map:     Platform → adapter mapping used to deliver DMs.
        """
        self._user_allowlist: set[str] | None = user_allowlist
        self._adapter_map: dict[str, Any] = dict(adapter_map or {})

        if pairing_store is not None:
            self._pairing_store: object | None = pairing_store
        elif pairing_db_path is not None:
            from arcgateway.pairing import PairingStore
            self._pairing_store = PairingStore(db_path=pairing_db_path)
        else:
            self._pairing_store = None

    # -----------------------------------------------------------------------
    # Allowlist management
    # -----------------------------------------------------------------------

    def is_user_approved(self, user_did: str) -> bool:
        """Return True if the user is approved or pairing enforcement is disabled.

        Args:
            user_did: The user's DID to check.

        Returns:
            True when approved or when allowlist is None (enforcement off).
        """
        if self._user_allowlist is None:
            return True
        return user_did in self._user_allowlist

    def add_approved_user(self, user_did: str) -> None:
        """Add a user DID to the allowlist (called after operator approval).

        Args:
            user_did: DID of the newly approved user.
        """
        if self._user_allowlist is None:
            self._user_allowlist = set()
        self._user_allowlist.add(user_did)
        _logger.info(
            "Pairing: user uid_h=%s added to allowlist",
            hash_user_did(user_did),
        )

    def remove_approved_user(self, user_did: str) -> None:
        """Remove a user DID from the allowlist (ban or re-pair).

        Args:
            user_did: DID to remove.
        """
        if self._user_allowlist is not None:
            self._user_allowlist.discard(user_did)

    def register_adapter(self, platform: str, adapter: Any) -> None:
        """Register a platform adapter for DM delivery.

        Args:
            platform: Platform name (e.g. "telegram", "slack").
            adapter:  Adapter instance implementing send().
        """
        self._adapter_map[platform] = adapter

    # -----------------------------------------------------------------------
    # Intercept
    # -----------------------------------------------------------------------

    async def handle_unpaired_user(self, event: InboundEvent) -> None:
        """Intercept a message from an unapproved user and issue a pairing code.

        Mints a new pairing code (or handles rate-limit / platform-full /
        platform-locked cases) and delivers the result via the platform adapter.

        Closes 5 TODO(M1 T1.7 integration) comments from the original SessionRouter
        implementation by calling adapter.send() with the pairing code DM.

        Uses duck-typing for the pairing store: any object with a ``mint_code()``
        coroutine is accepted (no isinstance guard).

        Args:
            event: Intercepted inbound event from an unapproved user.
        """
        uid_h = hash_user_did(event.user_did)

        if self._pairing_store is None:
            _logger.warning(
                "Pairing: no store — uid_h=%s platform=%s dropped",
                uid_h, event.platform,
            )
            return

        # Import pairing exceptions lazily to avoid top-level circular deps.
        from arcgateway.pairing import (
            PairingPlatformFull,
            PairingPlatformLocked,
            PairingRateLimited,
        )

        adapter = self._adapter_map.get(event.platform)

        try:
            # Duck-typed call: works with PairingStore or any compatible mock.
            pairing_code = await self._pairing_store.mint_code(  # type: ignore[attr-defined]
                platform=event.platform,
                platform_user_id=event.user_did,
            )
            _logger.info(
                "Pairing: minted code for uid_h=%s platform=%r (code hidden)",
                uid_h, event.platform,
            )
            # Deliver the pairing code via adapter DM (closes TODO M1 T1.7).
            if adapter is not None:
                await adapter.send(
                    event.chat_id,
                    f"To pair with this agent, share this code with your operator: "
                    f"{pairing_code.code}\n\n"
                    f"Code expires in 1 hour.\n"
                    f"Operator command: arc gateway pair approve {pairing_code.code}",
                )
            # Prevent accidental log inclusion via __repr__
            del pairing_code

        except PairingRateLimited:
            _logger.info(
                "Pairing: rate-limited uid_h=%s platform=%r",
                uid_h, event.platform,
            )
            # Re-send reminder (closes TODO M1 T1.7 rate-limited branch).
            if adapter is not None:
                await adapter.send(
                    event.chat_id,
                    "You already have a pending pairing code. "
                    "Please share it with your operator or wait for it to expire.",
                )

        except PairingPlatformFull:
            _logger.warning(
                "Pairing: platform %r full — uid_h=%s dropped",
                event.platform, uid_h,
            )
            # Notify user that pairing is at capacity (closes TODO M1 T1.7 full branch).
            if adapter is not None:
                await adapter.send(
                    event.chat_id,
                    "Pairing is temporarily unavailable. Please try again later.",
                )

        except PairingPlatformLocked:
            _logger.warning(
                "Pairing: platform %r locked — uid_h=%s dropped",
                event.platform, uid_h,
            )
            # Notify user that pairing is locked (closes TODO M1 T1.7 locked branch).
            if adapter is not None:
                await adapter.send(
                    event.chat_id,
                    "Pairing is currently locked due to suspicious activity. "
                    "Contact your operator.",
                )
