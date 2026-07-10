"""PairingInterceptor — pairing gate before session routing.

Holds the pairing-check and DM-delivery logic so SessionRouter's core
(race-guard + task spawn) stays free of pairing concerns.

Design (SDD §3.1 DM Pairing / T1.8):
    - Authorization is static allowlist OR store-approved: is_user_approved()
      checks the in-memory user_allowlist first, then falls through to a
      LIVE SQLite lookup via pairing_store.is_approved() — this is what
      makes `arc gateway pair approve` (a separate CLI process writing to
      the same db_path) take effect on the running gateway's next message,
      with no frozen in-memory list to go stale.
    - When not approved: mints a pairing code and DMs the user via the adapter.
    - Receives the adapter_map at construction time and calls adapter.send()
      directly, with a DeliveryTarget (not a raw chat_id) — adapters expect
      the same shape SessionRouter uses for turn replies.
    - No-op (all users approved) when BOTH user_allowlist and pairing_store
      are None — the enforcement-disabled default (require_pairing=false).
    - Duck-typed for testability: pairing_store only needs .mint_code() and
      .is_approved() coroutines.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcgateway.delivery import DeliveryTarget
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
        _user_allowlist: Set of approved user_did values, checked first (no
                         SQLite round-trip). None AND no pairing_store means
                         enforcement is disabled entirely (all approved).
        _pairing_store:  Optional store with mint_code() and is_approved()
                         coroutines — the live cross-process approval check.
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

    async def is_user_approved(self, user_did: str, platform: str) -> bool:
        """Return True if the user is approved to route to the agent.

        Authorization is static-allowlist OR store-approved:
          1. Enforcement disabled entirely (no allowlist AND no store) → True.
          2. In the static allowlist (``add_approved_user`` / construction-time
             ``user_allowlist``) → True. Checked first so operators/known
             users skip a SQLite round-trip entirely.
          3. Otherwise, when a pairing_store is wired, the LIVE SQLite
             ``pairing_approvals`` table is consulted via
             ``pairing_store.is_approved()`` — this is what makes
             ``arc gateway pair approve`` (a separate CLI process writing to
             the same db_path) take effect on the next message, with no
             frozen in-memory list to go stale.
          4. Otherwise: not approved.

        Args:
            user_did: The user's DID to check.
            platform: Platform the message arrived on (e.g. "telegram") —
                      required so the SQLite lookup hashes the same
                      (platform, user_did) pair ``mint_code()`` hashed.

        Returns:
            True when approved by either path.
        """
        if self._user_allowlist is None and self._pairing_store is None:
            return True
        if self._user_allowlist is not None and user_did in self._user_allowlist:
            return True
        if self._pairing_store is not None:
            is_approved = getattr(self._pairing_store, "is_approved", None)
            if is_approved is not None:
                return bool(await is_approved(platform, user_did))
        return False

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

        Uses duck-typing for the pairing store: any object with a ``mint_code()``
        coroutine is accepted (no isinstance guard).

        Args:
            event: Intercepted inbound event from an unapproved user.
        """
        uid_h = hash_user_did(event.user_did)

        if self._pairing_store is None:
            _logger.warning(
                "Pairing: no store — uid_h=%s platform=%s dropped",
                uid_h,
                event.platform,
            )
            return

        # Import pairing exceptions lazily to avoid top-level circular deps.
        from arcgateway.pairing import (
            PairingPlatformFull,
            PairingPlatformLocked,
            PairingRateLimited,
        )

        adapter = self._adapter_map.get(event.platform)
        # adapter.send()'s real Protocol (BasePlatformAdapter.send) takes a
        # DeliveryTarget, not a raw chat_id string. Building it once here
        # (instead of passing event.chat_id directly, as this method used
        # to) is what makes DM delivery work against a real adapter —
        # a plain str crashes on `target.chat_id` inside TelegramAdapter.send().
        target = DeliveryTarget(platform=event.platform, chat_id=event.chat_id)

        try:
            # Duck-typed call: works with PairingStore or any compatible mock.
            pairing_code = await self._pairing_store.mint_code(  # type: ignore[attr-defined]  # reason: _pairing_store is duck-typed (PairingStore or test mock); the `# Duck-typed` comment above is the design contract
                platform=event.platform,
                platform_user_id=event.user_did,
            )
            _logger.info(
                "Pairing: minted code for uid_h=%s platform=%r (code hidden)",
                uid_h,
                event.platform,
            )
            # Deliver the pairing code via adapter DM.
            if adapter is not None:
                await adapter.send(
                    target,
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
                uid_h,
                event.platform,
            )
            # Re-send reminder.
            if adapter is not None:
                await adapter.send(
                    target,
                    "You already have a pending pairing code. "
                    "Please share it with your operator or wait for it to expire.",
                )

        except PairingPlatformFull:
            _logger.warning(
                "Pairing: platform %r full — uid_h=%s dropped",
                event.platform,
                uid_h,
            )
            # Notify user that pairing is at capacity.
            if adapter is not None:
                await adapter.send(
                    target,
                    "Pairing is temporarily unavailable. Please try again later.",
                )

        except PairingPlatformLocked:
            _logger.warning(
                "Pairing: platform %r locked — uid_h=%s dropped",
                event.platform,
                uid_h,
            )
            # Notify user that pairing is locked.
            if adapter is not None:
                await adapter.send(
                    target,
                    "Pairing is currently locked due to suspicious activity. "
                    "Contact your operator.",
                )
