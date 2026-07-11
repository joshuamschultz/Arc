"""Task #34 — seed [platforms.*].allowed_user_ids into PairingInterceptor.

Live diagnosis: with require_pairing=true, a user in a platform's static
allowed_user_ids list still got forced through DM pairing on their first
message. Root cause: SessionRouter's PairingInterceptor._user_allowlist was
NEVER populated from config at either construction site (GatewayRunner.
from_config, bootstrap.build_for_embedded) — both passed pairing_store only.
The adapter-level static check passed (it reads allowed_user_ids directly),
but the router-level check always fell through to the SQLite pairing_store,
which has no row for a user who was never DM-paired.

``build_user_allowlist`` is the single function both construction sites call.
Each platform's allowed_user_ids entries are mapped into THAT platform's own
user_did scheme — telegram: "did:arc:telegram:{id}", slack: "slack:{id}" —
a deliberate, pre-existing inconsistency this fix does not unify.
"""

from __future__ import annotations

from arcgateway.config import GatewayConfig
from arcgateway.pairing_allowlist import build_user_allowlist


def _platforms(toml: str) -> object:
    return GatewayConfig.from_toml_str(toml).platforms


class TestBuildUserAllowlist:
    def test_telegram_ids_mapped_to_telegram_did_scheme(self) -> None:
        platforms = _platforms(
            """
[platforms.telegram]
enabled = true
allowed_user_ids = [111, 222]
"""
        )
        allowlist = build_user_allowlist(platforms)
        assert allowlist == {"did:arc:telegram:111", "did:arc:telegram:222"}

    def test_slack_ids_mapped_to_slack_did_scheme(self) -> None:
        platforms = _platforms(
            """
[platforms.slack]
enabled = true
allowed_user_ids = ["UABC123"]
"""
        )
        allowlist = build_user_allowlist(platforms)
        assert allowlist == {"slack:UABC123"}

    def test_both_platforms_combine_with_their_own_schemes(self) -> None:
        platforms = _platforms(
            """
[platforms.telegram]
enabled = true
allowed_user_ids = [111]

[platforms.slack]
enabled = true
allowed_user_ids = ["UABC123"]
"""
        )
        allowlist = build_user_allowlist(platforms)
        assert allowlist == {"did:arc:telegram:111", "slack:UABC123"}

    def test_disabled_platform_contributes_nothing(self) -> None:
        platforms = _platforms(
            """
[platforms.telegram]
enabled = false
allowed_user_ids = [111]
"""
        )
        assert build_user_allowlist(platforms) is None

    def test_platform_without_allowed_user_ids_contributes_nothing(self) -> None:
        platforms = _platforms(
            """
[platforms.telegram]
enabled = true
"""
        )
        assert build_user_allowlist(platforms) is None

    def test_mattermost_has_no_known_user_did_scheme_and_is_skipped(self) -> None:
        """Mattermost auth is channel-based (allowed_channel_ids), not user-based."""
        platforms = _platforms(
            """
[platforms.mattermost]
enabled = true
allowed_channel_ids = ["chan1"]
"""
        )
        assert build_user_allowlist(platforms) is None

    def test_no_platforms_configured_returns_none_not_empty_set(self) -> None:
        """None (not set()) preserves PairingInterceptor's "no allowlist AND no
        store => enforcement disabled" fast path for require_pairing=false
        deployments that never configured any allowed_user_ids — passing an
        empty set instead would flip that path from default-open to
        default-closed, denying every platform (a real regression).
        """
        platforms = _platforms("")
        assert build_user_allowlist(platforms) is None

    def test_web_platform_never_contributes(self) -> None:
        """Web has no allowed_user_ids concept and is not in remote_blocks()."""
        platforms = _platforms(
            """
[platforms.web]
enabled = true
"""
        )
        assert build_user_allowlist(platforms) is None
