"""``[host_requires.remote_login]`` — a two-step sign-in a browser can finish.

Some CLIs have a headless login: step one prints a consent URL, the person signs
in with the provider in their OWN browser, lands on a dead loopback redirect, and
pastes that address back; step two exchanges it. That is a sign-in Arc can drive
from a web page without pretending — so the manifest may declare it, and these
tests pin what makes the declaration safe to run:

* both steps or neither — a begin with no complete is a button that strands the
  operator half signed in;
* each step invokes the requirement's own binary and nothing else;
* the pasted address has exactly one reserved slot, ``{redirect_url}``, and only
  in the complete step; every other slot names a visible field, never a
  credential (the argv rule every declared command already obeys);
* a consent host is named, so Arc hands the operator a link to that host only;
* the expired pattern compiles and has a check to read.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arcagent.core.tier import Tier
from arcagent.extension.manifest import HostRequirement, RemoteLogin, load_manifest

_BEGIN = "gog auth add {account} --remote --step 1 --force-consent --no-input"
_COMPLETE = (
    "gog auth add {account} --remote --step 2 --force-consent --no-input --auth-url {redirect_url}"
)


def _manifest(remote: str, *, secrets: str = "") -> str:
    fields = secrets or (
        '[[secrets]]\nname = "account"\nsensitive = false\nrequired = false\nformat = "email"\n'
    )
    return f"""
[extension]
name = "g"
version = "1.0.0"
attachment = "cli"

[[host_requires]]
name = "gog"
authorize_command = "gog auth add"
verify_command = "gog gmail labels get INBOX --plain"
verify_pattern = "INBOX"

{remote}

{fields}

[tools]
allow = []
"""


def _remote(
    begin: str = _BEGIN, complete: str = _COMPLETE, host: str = "accounts.google.com"
) -> str:
    return (
        "[host_requires.remote_login]\n"
        f"begin = {begin!r}\ncomplete = {complete!r}\nconsent_host = {host!r}\n"
    ).replace("'", '"')


def test_a_well_formed_remote_login_parses() -> None:
    manifest = load_manifest(_manifest(_remote()), tier=Tier.PERSONAL)
    login = manifest.host_requires[0].remote_login
    assert login is not None
    assert login.consent_host == "accounts.google.com"


def test_a_requirement_without_one_has_none() -> None:
    manifest = load_manifest(_manifest(""), tier=Tier.PERSONAL)
    assert manifest.host_requires[0].remote_login is None


def test_a_step_that_runs_another_program_is_refused() -> None:
    with pytest.raises(ValidationError, match="must invoke gog"):
        load_manifest(_manifest(_remote(begin="sh -c {account}")), tier=Tier.PERSONAL)


def test_the_complete_step_must_carry_the_pasted_address() -> None:
    with pytest.raises(ValidationError, match="redirect_url"):
        load_manifest(
            _manifest(_remote(complete="gog auth add {account} --remote --step 2")),
            tier=Tier.PERSONAL,
        )


def test_the_begin_step_may_not_ask_for_the_pasted_address() -> None:
    with pytest.raises(ValidationError, match="redirect_url"):
        load_manifest(
            _manifest(_remote(begin="gog auth add {account} --auth-url {redirect_url}")),
            tier=Tier.PERSONAL,
        )


def test_the_pasted_address_must_be_one_whole_argument() -> None:
    """Glued to other text it could be read as a flag or split a value."""
    with pytest.raises(ValidationError, match="its own argument"):
        load_manifest(
            _manifest(
                _remote(
                    complete="gog auth add {account} --remote --step 2 --auth-url=x{redirect_url}"
                )
            ),
            tier=Tier.PERSONAL,
        )


def test_a_step_naming_a_credential_is_refused() -> None:
    secrets = '[[secrets]]\nname = "token"\n'
    with pytest.raises(ValidationError, match="credential"):
        load_manifest(
            _manifest(
                _remote(begin="gog auth add {token} --remote --step 1"),
                secrets=secrets,
            ),
            tier=Tier.PERSONAL,
        )


def test_a_step_naming_an_undeclared_field_is_refused() -> None:
    with pytest.raises(ValidationError, match="not a field"):
        load_manifest(
            _manifest(_remote(begin="gog auth add {region} --remote --step 1")),
            tier=Tier.PERSONAL,
        )


def test_the_consent_host_is_a_bare_hostname() -> None:
    with pytest.raises(ValidationError):
        RemoteLogin(begin="gog a", complete="gog b {redirect_url}", consent_host="https://evil/")


def test_an_expired_pattern_needs_a_check_and_must_compile() -> None:
    with pytest.raises(ValidationError, match="verify_command"):
        HostRequirement(name="gog", verify_expired_pattern="invalid_grant")
    with pytest.raises(ValidationError, match="regex"):
        HostRequirement(name="gog", verify_command="gog x", verify_expired_pattern="(")


def test_an_unknown_key_in_the_table_is_refused() -> None:
    with pytest.raises(ValidationError):
        load_manifest(
            _manifest(_remote() + 'shell = "yes"\n'),
            tier=Tier.PERSONAL,
        )
