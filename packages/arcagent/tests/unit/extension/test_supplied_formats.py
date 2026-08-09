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
    with pytest.raises(ExtensionError) as raised:
        _url(value)
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
        load_manifest(
            _MANIFEST.replace('format = "https_url"\n', ""), tier=Tier.PERSONAL
        ).secrets[0].format
        == ""
    )


def test_a_format_core_does_not_implement_is_refused_at_parse_time() -> None:
    """A shape nothing enforces is a control an operator believes is in force."""
    with pytest.raises(ValidationError):
        load_manifest(_MANIFEST.replace('"https_url"', '"email_address"'), tier=Tier.PERSONAL)
