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

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arcgateway.config import GatewayConfig, PlatformsSection
    from arcgateway.pairing import Tier

_logger = logging.getLogger("arcgateway.pairing_allowlist")

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


def build_team_agent_allowlist(team_root: Path) -> set[str]:
    """Seed a pairing allowlist from every same-team agent's DID.

    Task: an agent DM-ing a teammate on the SAME ``team_root`` was forced
    through the ``arc gateway pair approve`` dance — its DID reached
    ``PairingInterceptor`` with no allowlist entry, so a code was minted.
    Same-team agents share one trusted operator; they are always-ready peers,
    not unknown external users. Reading each agent's ``[identity].did`` from its
    ``arcagent.toml`` (the same signal ``team_roster``/``bootstrap`` discover
    agents by) pre-approves every teammate for agent-to-agent routing.

    Only same-team agents are seeded — an EXTERNAL or cross-system entity still
    faces pairing, so this never weakens the gate for untrusted callers. Returns
    an empty set (never ``None``) when no agent declares a DID; the caller merges
    it into the allowlist only while ``require_pairing`` is on, so the "no
    allowlist AND no store => enforcement disabled" fast path is never affected.
    """
    if not team_root.exists():
        return set()
    try:
        import tomllib
    except ImportError:  # pragma: no cover — Python <3.11 fallback
        import tomli as tomllib  # type: ignore[no-redef]  # reason: Python <3.11 fallback — tomli mirrors stdlib tomllib

    dids: set[str] = set()
    for toml_path in sorted(team_root.glob("*/arcagent.toml")):
        try:
            cfg = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            _logger.warning("pairing allowlist: unreadable agent config %s", toml_path)
            continue
        identity = cfg.get("identity") if isinstance(cfg.get("identity"), dict) else {}
        did = identity.get("did") if isinstance(identity, dict) else None
        if isinstance(did, str) and did:
            dids.add(did)
    return dids


def build_pairing_wiring(config: GatewayConfig, tier: Tier) -> tuple[Any | None, set[str] | None]:
    """Build the PairingStore + static allowlist for ``GatewayRunner.from_config``.

    ``[security].require_pairing`` activates DM pairing enforcement: a
    PairingStore is built from ``[pairing].db_path`` and wired into the
    SessionRouter's PairingInterceptor. Left unset (the default), both
    return values are ``None`` — the interceptor is a no-op, matching every
    deployment's current behaviour until pairing is explicitly opted into.

    The static ``user_allowlist`` is seeded ONLY when ``require_pairing`` is
    on, not unconditionally: seeding it while ``require_pairing=false``
    would make PairingInterceptor start denying non-allowlisted users from
    OTHER platforms (e.g. web) that reach SessionRouter with no
    adapter-level allowlist gate of their own — a regression for the very
    deployments this branch is not supposed to touch (task #34).
    """
    if not config.security.require_pairing:
        return None, None

    from arcgateway.pairing import PairingStore

    pairing_store: Any = PairingStore(db_path=config.pairing.db_path, tier=tier)
    user_allowlist = build_user_allowlist(config.platforms)
    return pairing_store, user_allowlist


__all__ = ["build_pairing_wiring", "build_team_agent_allowlist", "build_user_allowlist"]
