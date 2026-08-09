"""A person types what a person types, and the tool meets them there.

The live failure this exists for. An operator typed a Jira address the way
anybody would — ``ctgfederal.com.atlassian.net`` — and got:

    probe: jira did not answer — ctgfederal.com.atlassian.net did not answer:
    Request URL is missing an 'http://' or 'https://' protocol.

Three separate defects in one line. A bare hostname is what a person types and
knowing that a URL needs a scheme is the tool's job, not theirs. The refusal was
httpx's sentence, shown raw to somebody who cannot act on it. And it arrived at
PROBE — after the credential had been written and rolled back — rather than while
they were still looking at the form.

So a bundle declares the SHAPE of a field it asks for, and the value is put in
that shape where it enters:

* ``format = "https_url"`` accepts a bare host and makes it ``https://<host>``.
* It never invents ``http://``. A credential travels over this connection on
  every call, so an explicit ``http://`` is refused rather than honoured — see
  :func:`~arcagent.extension.field_formats.normalize`.
* The refusal is Arc's own words, names the field, and says what to do.

Kept general on purpose: ``https_url`` describes web addresses, not Atlassian.
Core learns no vendor here (REQ-280), and a ``cli`` or ``mcp`` bundle — which has
no code of its own to normalise anything with — gets this by declaring it.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.field_formats import normalize
from arcagent.extension.manifest import load_manifest

#: What the operator actually typed.
_TYPED = "ctgfederal.com.atlassian.net"
_EXPECTED = "https://ctgfederal.com.atlassian.net"


def _url(value: str) -> str:
    return normalize("https_url", "base_url", value)


def _refusal(value: str) -> str:
    return _refusal_for(_url, value)


def _refusal_for(shape: Callable[[str], str], value: str) -> str:
    """The message an operator reads when one shape refuses one value."""
    with pytest.raises(ExtensionError) as raised:
        shape(value)
    return raised.value.message


# --- the reported input -------------------------------------------------------


def test_the_address_the_operator_typed_is_accepted() -> None:
    """The exact value from the report, and the exact value it must become."""
    assert _url(_TYPED) == _EXPECTED


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        (_TYPED, _EXPECTED),
        (f"{_TYPED}/", _EXPECTED),
        (f"https://{_TYPED}", _EXPECTED),
        (f"https://{_TYPED}/", _EXPECTED),
        (f"  {_TYPED}  ", _EXPECTED),
        (f"HTTPS://{_TYPED}", _EXPECTED),
        (f"https://{_TYPED}/wiki/", f"{_EXPECTED}/wiki"),
    ],
)
def test_every_way_a_person_writes_the_same_address_reaches_the_same_place(
    typed: str, expected: str
) -> None:
    """Trailing slash, spaces, and a shouted scheme are the same address.

    A path is kept — some deployments live under one — but its trailing slash
    goes, so ``.../wiki`` and ``.../wiki/`` cannot become two different
    connections to the same site.
    """
    assert _url(typed) == expected


# --- what it will not do ------------------------------------------------------


def test_a_bare_host_is_never_upgraded_to_plaintext() -> None:
    """The default must be the safe one, because it is the one people get."""
    assert _url(_TYPED).startswith("https://")


def test_an_explicit_http_address_is_refused_and_says_why() -> None:
    """DECISION: refuse, do not silently upgrade and do not accept.

    A credential goes over this connection on every single call, so plaintext
    here puts an API token on the wire. Silently rewriting it to ``https`` would
    be worse than refusing: an operator who deliberately typed ``http`` because
    that is what their host serves would get a connection that fails later for a
    reason nothing told them about. So it is refused, and the refusal says what
    is wrong and what to do.

    The escape hatch is not a flag in core: a bundle whose service really is
    plaintext declares no ``format`` and gets the value through untouched.
    """
    message = _refusal(f"http://{_TYPED}")

    assert "http://" in message
    assert "https://" in message
    assert "base_url" in message


@pytest.mark.parametrize(
    "typed",
    ["ftp://files.example.net", "file:///etc/passwd", "javascript:alert(1)", "://example.net"],
)
def test_an_address_that_is_not_the_web_is_refused(typed: str) -> None:
    """Only a web address can be a web address."""
    assert "base_url" in _refusal(typed)


def test_an_address_carrying_a_password_is_refused() -> None:
    """``https://user:pass@host`` would smuggle a credential into a visible field.

    This field is declared non-sensitive, so it is shown in the form and read
    back on the auth verb. A value of this shape would put a password in both,
    which is the leak the sensitivity split exists to prevent — so the shape is
    refused rather than stored.
    """
    message = _refusal(f"https://someone:hunter2@{_TYPED}")

    assert "hunter2" not in message, "a refusal must not echo what it refused"
    assert "base_url" in message


@pytest.mark.parametrize("typed", ["", "   ", "https://", "https:// ", "has a space.example.net"])
def test_a_value_that_names_no_host_is_refused(typed: str) -> None:
    assert "base_url" in _refusal(typed)


def test_the_refusal_is_arcs_own_words_and_names_the_next_step() -> None:
    """httpx's sentence was shown raw to a non-technical operator; ours is written for one."""
    message = _refusal("")

    assert "Request URL" not in message, "that phrasing is httpx's, not ours"
    assert "base_url" in message
    assert message.endswith(".") or message.endswith("'")


# --- a field with no declared format is untouched ------------------------------


@pytest.mark.parametrize("value", ["  a token with spaces  ", "ATATT//not-a-url", "hunter2"])
def test_a_field_declaring_no_format_is_passed_through_exactly(value: str) -> None:
    """A token is not a URL and must not be trimmed, lower-cased, or reshaped.

    Normalising by default would corrupt a credential whose leading or trailing
    characters are significant, and the bug would show up as a 401 nobody could
    explain.
    """
    assert normalize("", "api_token", value) == value


# --- a pasted credential ------------------------------------------------------
#
# The second live failure. An operator connected Jira with a real token, a real
# email and the correct address, and got:
#
#     probe: jira did not answer — https://ctgfederal.atlassian.net did not answer:
#     Client error '401 Unauthorized' for url '…/rest/api/3/myself'
#
# A value declaring no format is stored byte for byte, which is right for a
# password and wrong for a token: an API token copied out of a browser dialog can
# carry a trailing space or a zero-width character the operator cannot see, and
# there is no character it could carry that is meant to be there. So the bundle
# says which of its fields are that shape, and core acts only on the ones that do.

#: A token as Atlassian shows it. The sentinel must never appear in a refusal.
_TOKEN = "ATATT3xFfGF0-sentinel-000"


def _token(value: str) -> str:
    return normalize("api_token", "api_token", value)


def _email(value: str) -> str:
    return normalize("email", "email", value)


@pytest.mark.parametrize(
    "typed",
    [
        f"  {_TOKEN}  ",
        f"{_TOKEN}\n",
        f"\t{_TOKEN}\r\n",
        f"\u200b{_TOKEN}\ufeff",
        f"\u00a0{_TOKEN}\u00a0",
    ],
)
def test_a_token_arrives_clean_however_the_paste_carried_it(typed: str) -> None:
    """Every way a browser hands over a copied token, and the one value they mean.

    The zero-width space and the byte-order mark are the cases that made this
    unexplainable: they cost the operator a 401 while the field on screen looked
    exactly right.
    """
    assert _token(typed) == _TOKEN


@pytest.mark.parametrize("typed", [f"{_TOKEN} {_TOKEN}", f"AT\u200bATT{_TOKEN}", "one two"])
def test_a_token_with_something_invisible_in_the_middle_is_refused(typed: str) -> None:
    """Trimming the ends is safe; silently editing the middle is not.

    A character inside the value could be significant, so it is refused rather
    than removed — and the refusal says a character is there, which is the one
    thing the operator cannot see for themselves.
    """
    message = _refusal_for(_token, typed)

    assert "api_token" in message
    assert _TOKEN not in message, "a refusal must not echo the credential"


def test_a_token_that_was_nothing_but_whitespace_is_refused() -> None:
    assert "api_token" in _refusal_for(_token, "   \n  ")


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("  operator@ctgfederal.example ", "operator@ctgfederal.example"),
        ("operator@ctgfederal.example\n", "operator@ctgfederal.example"),
        ("\u200boperator@ctgfederal.example", "operator@ctgfederal.example"),
    ],
)
def test_an_email_arrives_clean_however_the_paste_carried_it(typed: str, expected: str) -> None:
    """The other half of a basic credential, and the other half of the same 401."""
    assert _email(typed) == expected


@pytest.mark.parametrize("typed", ["operator", "operator@", "@ctgfederal.example", "a@b c@d"])
def test_a_value_that_is_not_an_address_is_refused_before_anything_is_stored(typed: str) -> None:
    """A refusal at the form beats a 401 after a credential has been written."""
    assert "email" in _refusal_for(_email, typed)


def test_the_shapes_are_only_applied_to_the_fields_that_declare_them() -> None:
    """The safety rail on all of the above: silence still means byte for byte.

    A password may legitimately end in a space. Core does not guess which of a
    service's fields is which — the bundle says so, in the bundle.
    """
    assert normalize("", "password", "  hunter2  ") == "  hunter2  "


# --- what a bundle may declare ------------------------------------------------


_MANIFEST = """
[extension]
name = "acme"
version = "1.0.0"
attachment = "native"
description = "Acme."

[config.native]
entrypoint = "acme_attachment"

[[secrets]]
name = "base_url"
prompt = "Your Acme web address, exactly as it appears in the browser bar."
sensitive = false
format = "https_url"

[tools]
allow = ["ping"]

[[tools.declared]]
name = "ping"
classification = "read_only"
"""


def test_a_bundle_declares_the_shape_of_the_field_it_asks_for() -> None:
    assert load_manifest(_MANIFEST, tier=Tier.PERSONAL).secrets[0].format == "https_url"


def test_a_field_declaring_nothing_has_no_format() -> None:
    assert load_manifest(_MANIFEST.replace('format = "https_url"\n', ""), tier=Tier.PERSONAL)
    assert (
        load_manifest(_MANIFEST.replace('format = "https_url"\n', ""), tier=Tier.PERSONAL)
        .secrets[0]
        .format
        == ""
    )


@pytest.mark.parametrize("shape", ["https_url", "api_token", "email"])
def test_every_shape_core_enforces_can_be_declared_by_a_bundle(shape: str) -> None:
    """A shape core implements and no manifest can ask for is a shape nothing uses."""
    manifest = load_manifest(_MANIFEST.replace('"https_url"', f'"{shape}"'), tier=Tier.PERSONAL)

    assert manifest.secrets[0].format == shape


def test_a_format_core_does_not_implement_is_refused_at_parse_time() -> None:
    """A shape nothing enforces is a control an operator believes is in force."""
    with pytest.raises(ValidationError):
        load_manifest(_MANIFEST.replace('"https_url"', '"iso_date"'), tier=Tier.PERSONAL)
