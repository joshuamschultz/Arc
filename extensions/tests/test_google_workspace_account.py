"""Each Google connection binds one account, so gog reads the right mailbox.

`gog` stores every account `gog auth add` signs in, and refuses a bare read with
"missing --account (or set GOG_ACCOUNT)" the moment more than one exists. A
connection carries which account it is through `[secrets.placement] GOG_ACCOUNT`,
so every gog process this connection starts already acts as exactly that address
-- two connections, two accounts, no crossing, and no per-call --account to thread
through the sync path. (The keyring password gog also needs in a headless service
rides GOG_KEYRING_PASSWORD in the service environment, which the child inherits.)
"""

from __future__ import annotations

from pathlib import Path

from arcagent.core.tier import Tier
from arcagent.extension.manifest import load_manifest
from arcagent.extension.secrets import Secret
from arcagent.modules.connectors.install import placement_environment

BUNDLE = Path(__file__).resolve().parents[1] / "google_workspace"


def _manifest():  # type: ignore[no-untyped-def]
    return load_manifest((BUNDLE / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL)


def test_the_account_is_placed_into_gog_account() -> None:
    env = placement_environment(_manifest(), {"account": Secret("josh@blackarcindustrial.com")})
    assert set(env) == {"GOG_ACCOUNT"}
    assert env["GOG_ACCOUNT"].reveal() == "josh@blackarcindustrial.com"


def test_two_connections_bind_two_distinct_accounts() -> None:
    manifest = _manifest()
    industrial = placement_environment(manifest, {"account": Secret("josh@blackarcindustrial.com")})
    systems = placement_environment(manifest, {"account": Secret("josh@blackarcsystems.com")})
    assert industrial["GOG_ACCOUNT"].reveal() == "josh@blackarcindustrial.com"
    assert systems["GOG_ACCOUNT"].reveal() == "josh@blackarcsystems.com"
