"""``[tools.routing]`` and ``[tools.read_only]`` — one tool name, many accounts.

A bundle that can be connected more than once (several Google mailboxes, several
Jira sites) declares which tool argument SELECTS the connection and which
connection field it is matched against. The manifest rules pinned here are what
keep that selector from becoming a second route onto argv or a credential oracle.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arcagent.core.tier import Tier
from arcagent.extension.manifest import load_manifest


def _manifest(tools_extra: str, *, secrets: str = "", command_args: str = "") -> str:
    fields = secrets or (
        '[[secrets]]\nname = "account"\nsensitive = false\nrequired = false\nformat = "email"\n'
        '[secrets.placement]\nvariable = "SVC_ACCOUNT"\n'
        '[[secrets]]\nname = "read_only"\nsensitive = false\nrequired = false\n'
        'choices = ["yes", "no"]\ndefault = "yes"\n'
    )
    return f"""
[extension]
name = "svc"
version = "1.0.0"
attachment = "cli"

{fields}

[tools]
allow = ["svc_read"]
{tools_extra}

[[tools.declared]]
name = "svc_read"
classification = "read_only"

[config.cli]
binary = "svc"

[[config.cli.commands]]
tool = "svc_read"
argv = ["read"]
{command_args}
"""


_ROUTING = '[tools.routing]\nargument = "account"\nfield = "account"\nmatch = "email"\n'


def test_a_routing_declaration_parses() -> None:
    manifest = load_manifest(_manifest(_ROUTING), tier=Tier.PERSONAL)
    routing = manifest.tools.routing
    assert routing is not None
    assert (routing.argument, routing.field, routing.match) == ("account", "account", "email")


def test_the_selector_must_match_a_declared_visible_field() -> None:
    with pytest.raises(ValidationError, match="not a field"):
        load_manifest(
            _manifest(_ROUTING.replace('field = "account"', 'field = "site"')), tier=Tier.PERSONAL
        )
    secrets = '[[secrets]]\nname = "account"\n[secrets.placement]\nvariable = "SVC_ACCOUNT"\n'
    with pytest.raises(ValidationError, match="credential"):
        load_manifest(_manifest(_ROUTING, secrets=secrets), tier=Tier.PERSONAL)


def test_a_command_may_not_declare_the_selector_as_its_own_argument() -> None:
    """Otherwise the selector would reach argv as whatever the model typed."""
    args = '[[config.cli.commands.arguments]]\nname = "account"\nflag = "--account"\n'
    with pytest.raises(ValidationError, match="selector"):
        load_manifest(_manifest(_ROUTING, command_args=args), tier=Tier.PERSONAL)


def test_a_refusal_pattern_must_compile() -> None:
    with pytest.raises(ValidationError, match="regex"):
        load_manifest(_manifest(_ROUTING + 'refuse_values = "("\n'), tier=Tier.PERSONAL)


def test_read_only_names_a_visible_field_with_that_choice() -> None:
    ok = load_manifest(
        _manifest('[tools.read_only]\nfield = "read_only"\nwhen = "yes"\n'), tier=Tier.PERSONAL
    )
    assert ok.tools.read_only is not None
    with pytest.raises(ValidationError, match="not one of"):
        load_manifest(
            _manifest('[tools.read_only]\nfield = "read_only"\nwhen = "maybe"\n'),
            tier=Tier.PERSONAL,
        )
    with pytest.raises(ValidationError, match="not a field"):
        load_manifest(
            _manifest('[tools.read_only]\nfield = "mode"\nwhen = "yes"\n'), tier=Tier.PERSONAL
        )
