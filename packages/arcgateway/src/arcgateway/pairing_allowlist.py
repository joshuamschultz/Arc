"""Static ``allowed_user_ids`` → ``PairingInterceptor`` allowlist seeding.

Task #34 root cause: ``[platforms.<name>].allowed_user_ids`` exists so an
operator can pre-authorize known users without a DM-pairing round trip. But
``SessionRouter``'s ``PairingInterceptor`` never received it — both
``GatewayRunner.from_config`` and ``bootstrap.build_for_embedded`` constructed
``SessionRouter`` with ``pairing_store`` only, leaving ``_user_allowlist``
permanently ``None``. Live diagnosis: config was correct and the adapter's own
static check passed, but the router-level check always fell through to the
SQLite ``pairing_store`` (no row for a user never DM-paired) — so an
allowlisted user still got a pairing code minted on their first message.

Each platform's ``InboundEvent.user_did`` is built by that platform's OWN
adapter package, in its own scheme — the gateway core stays platform-agnostic
(see ``PlatformsSection``'s docstring; ``extra="allow"`` blocks are handed to
adapter plugins raw). Telegram: ``"did:arc:telegram:{user_id}"`` (arcgateway_
telegram/adapter.py). Slack: ``"slack:{user_id}"`` (arcgateway_slack/adapter.
py). This is a deliberate, PRE-EXISTING inconsistency — this fix matches each
platform's scheme, it does not unify them.

Mattermost is channel-based (``allowed_channel_ids``, not ``allowed_user_ids``
— a different auth model) and has no user_did scheme here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arcgateway.config import PlatformsSection

# platform name -> user_did formatter, matching that platform's OWN adapter
# scheme exactly (see module docstring). Deliberately NOT unified.
_USER_DID_SCHEMES: dict[str, Callable[[object], str]] = {
    "telegram": lambda user_id: f"did:arc:telegram:{user_id}",
    "slack": lambda user_id: f"slack:{user_id}",
}


def build_user_allowlist(platforms: PlatformsSection) -> set[str] | None:
    """Seed a static allowlist from every enabled platform's ``allowed_user_ids``.

    Returns ``None`` (never an empty set) when no platform contributes any
    ID — this preserves ``PairingInterceptor``'s "no allowlist AND no store
    => enforcement disabled" fast path for a deployment that never configured
    ``allowed_user_ids`` anywhere. Passing an empty set instead would flip
    that fast path from default-open to default-closed, denying every
    platform — a regression this function must never cause.
    """
    allowlist: set[str] = set()
    for name, block in platforms.remote_blocks().items():
        if not block.get("enabled"):
            continue
        formatter = _USER_DID_SCHEMES.get(name)
        if formatter is None:
            continue
        raw_ids = block.get("allowed_user_ids") or []
        allowlist.update(formatter(raw_id) for raw_id in raw_ids)
    return allowlist or None


__all__ = ["build_user_allowlist"]
