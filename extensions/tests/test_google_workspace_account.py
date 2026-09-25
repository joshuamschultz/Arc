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
    return load_manifest(
        (BUNDLE / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL
    )


def test_the_account_is_placed_into_gog_account() -> None:
    env = placement_environment(_manifest(), {"account": Secret("josh@blackarcindustrial.com")})
    assert set(env) == {"GOG_ACCOUNT"}
    assert env["GOG_ACCOUNT"].reveal() == "josh@blackarcindustrial.com"


def test_two_connections_bind_two_distinct_accounts() -> None:
    manifest = _manifest()
    industrial = placement_environment(
        manifest, {"account": Secret("josh@blackarcindustrial.com")}
    )
    systems = placement_environment(manifest, {"account": Secret("josh@blackarcsystems.com")})
    assert industrial["GOG_ACCOUNT"].reveal() == "josh@blackarcindustrial.com"
    assert systems["GOG_ACCOUNT"].reveal() == "josh@blackarcsystems.com"


def test_a_single_account_host_places_no_gog_account() -> None:
    # gog uses its one token with no --account, so an unset account must place
    # nothing rather than an empty GOG_ACCOUNT gog would misread.
    assert placement_environment(_manifest(), {}) == {}


# --- the standard tool set --------------------------------------------------------

_SENDS = {
    "google_gmail_send",
    "google_gmail_draft_send",
    "google_gmail_reply",
    "google_gmail_reply_all",
    "google_gmail_forward",
}


def _commands():  # type: ignore[no-untyped-def]
    from arcagent.extension.cli_attachment import CliCommand

    return [CliCommand.model_validate(raw) for raw in _manifest().config["cli"]["commands"]]


def test_every_google_call_is_routed_and_read_only_is_a_switch() -> None:
    manifest = _manifest()
    routing = manifest.tools.routing
    assert routing is not None
    assert (routing.argument, routing.field) == ("account", "account")
    assert manifest.tools.read_only is not None


def test_no_command_can_be_told_another_account_client_or_home() -> None:
    forbidden = ("--account", "-a", "--client", "--home", "--access-token", "--attach")
    for command in _commands():
        flags = [argument.flag for argument in command.arguments]
        assert not [flag for flag in flags if flag in forbidden], command.tool
        fixed = [token for token in command.argv if token.split("=")[0] in forbidden]
        assert not fixed, command.tool


def test_only_the_send_verbs_are_egress_and_every_read_is_read_only() -> None:
    declared = {tool.name: tool for tool in _manifest().tools.declared}
    for command in _commands():
        tool = declared[command.tool]
        egress = "network_egress" in tool.capability_tags
        assert egress == (command.tool in _SENDS), command.tool
        assert command.classification == tool.classification, command.tool
        if tool.classification == "read_only":
            assert "--readonly" in command.argv, command.tool
        else:
            assert "--readonly" not in command.argv, command.tool


def test_every_page_size_is_bounded() -> None:
    for command in _commands():
        for argument in command.arguments:
            if argument.flag == "--max":
                assert argument.maximum is not None, command.tool
