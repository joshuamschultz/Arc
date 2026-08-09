"""The shape a bundle says one of its fields has, applied where the value enters.

An operator typed a service's address the way anybody would — a bare hostname,
because that is what a browser bar shows them — and the connection failed at probe
with the HTTP library's own sentence, *"Request URL is missing an 'http://' or
'https://' protocol"*, shown raw to somebody who could do nothing with it. Knowing
that a URL needs a scheme is the tool's job, and a value that cannot work should be
refused while the operator is still looking at the form, not after a credential has
been written and rolled back.

So ``[[secrets]]`` may declare a ``format``, and it is applied at the boundary the
value crosses.

**This is general, not vendor knowledge (REQ-280).** ``https_url`` describes web
addresses; it says nothing about any service. It lives in core rather than in the
two bundles that need it today for two reasons: a ``cli`` or ``mcp`` bundle has no
code of its own to normalise anything with, and a rule copied into every bundle
that takes a URL is a rule that drifts in the one nobody looked at.

**On ``http://``: refused, not upgraded, not accepted.** A credential travels over
this connection on every call, so plaintext puts a token on the wire. Silently
rewriting it to ``https`` is the worse of the two failures — an operator whose
host really does serve plaintext would get a connection that breaks later with
nothing having told them why. A bundle whose service genuinely is plaintext
declares no ``format`` and its value passes through untouched, so the escape
hatch is a bundle's decision rather than a flag in core.

The same boundary answers the second reported failure. An operator pasted a real
API token beside a real sign-in address and got ``401 Unauthorized`` and a link to
MDN. A token copied out of a browser dialog can carry a trailing newline, a space,
or a zero-width character, none of which the operator can see and none of which is
ever part of the value — and a field declaring no format is stored byte for byte,
so the invisible character reached the service and came back as that 401.

**Which fields that is true of is the bundle's word, not a guess.** A password may
legitimately end in a space, so core does not trim by default; ``api_token`` and
``email`` are the declarations by which a bundle says this field is not one of
those. Both trim the ends and REFUSE anything invisible left in the middle: the
ends are where a paste picks characters up, and the middle is where a character
could still be meaningful.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal
from urllib.parse import urlsplit

from arcagent.core.errors import ExtensionError

#: The shapes core knows how to enforce. An empty format is "whatever the operator
#: typed, exactly" — the right answer for a password, whose leading and trailing
#: characters may be significant and which normalising would corrupt into a 401
#: nobody can explain. A closed set on purpose: a shape nothing enforces is a
#: control an operator believes is in force, so an unknown one is a manifest error.
SuppliedFormat = Literal["", "https_url", "api_token", "email"]

#: Unicode categories carrying no visible mark: controls and format characters
#: (where a browser's zero-width space and byte-order mark live) and every kind of
#: space separator (where the non-breaking space a copied line brings along lives).
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp", "Zs"})

#: Refusal code every format failure carries, so a surface can branch on the kind
#: of problem rather than on the wording an operator reads.
FIELD_FORMAT_INVALID = "SUPPLIED_VALUE_INVALID"

#: A host that could be typed into a browser bar: labels of letters, digits and
#: hyphens, optionally a port. Deliberately permissive about TLDs — ``localhost``
#: and an internal name are legitimate — and deliberately strict about whitespace,
#: which is what a pasted line-wrapped address arrives with.
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9-]+)*(:[0-9]{1,5})?$")


def normalize(supplied_format: str, field: str, value: str) -> str:
    """Put one supplied value into the shape its bundle declared.

    Args:
        supplied_format: The manifest's ``format`` for this field. Empty returns
            ``value`` unchanged, byte for byte.
        field: The declared field name, so a refusal names what to go and fix.
        value: What the operator typed.

    Returns:
        The value as it will be stored and used.

    Raises:
        ExtensionError: The value cannot be put in that shape. ``.message`` is
            written for the person who typed it and never echoes what they typed —
            a rejected address can carry a password, and the refusal is rendered
            in a browser and written to a log.
    """
    if supplied_format == "https_url":
        return _https_url(field, value)
    if supplied_format == "api_token":
        return _visible(field, value)
    if supplied_format == "email":
        return _email(field, value)
    return value


def _visible(field: str, value: str) -> str:
    """One run of visible characters: trimmed at the ends, refused in the middle.

    Trimming is safe where a paste picks characters up; editing the middle is not,
    because a character there could be part of the value. So the middle is refused
    — and the refusal has to say a character is present, since it is the one thing
    the operator cannot see for themselves.
    """
    trimmed = _trim_invisible(value)
    if not trimmed:
        return _refuse(field, "it is empty", "paste it again")
    if any(_is_invisible(character) for character in trimmed):
        return _refuse(
            field,
            "it has a space or an invisible character inside it",
            "copy it again, selecting only the value itself",
        )
    return trimmed


def _is_invisible(character: str) -> bool:
    return unicodedata.category(character) in _INVISIBLE_CATEGORIES


def _trim_invisible(value: str) -> str:
    """Everything from the first visible character to the last."""
    visible = [index for index, character in enumerate(value) if not _is_invisible(character)]
    return value[visible[0] : visible[-1] + 1] if visible else ""


def _email(field: str, value: str) -> str:
    """An email address: visible characters, and the one ``@`` that makes it one."""
    trimmed = _visible(field, value)
    local, separator, host = trimmed.partition("@")
    if not (separator and local and host) or "@" in host:
        return _refuse(
            field,
            "it is not an email address",
            "type the address you sign in with, like you@yourcompany.example.com",
        )
    return trimmed


def _https_url(field: str, value: str) -> str:
    """A web address over TLS, from whatever a person reasonably typed."""
    typed = value.strip()
    if not typed:
        return _refuse(field, "it is empty", "paste the web address you see in your browser bar")

    scheme, _, rest = typed.partition("://")
    if not rest:
        # No scheme at all — the reported case. This is what a person types.
        return _assembled(field, "https", typed)
    lowered = scheme.lower()
    if lowered == "http":
        return _refuse(
            field,
            "it starts with http://, which is not encrypted",
            "your credential is sent over this connection every time it is used, so "
            "change it to https://",
        )
    if lowered != "https":
        return _refuse(
            field,
            f"{scheme}:// is not a web address",
            "paste the address you see in your browser bar; it should start with https://",
        )
    return _assembled(field, "https", rest)


def _assembled(field: str, scheme: str, rest: str) -> str:
    """Validate the host and path of a scheme-less remainder, then put it together."""
    host, slash, path = rest.partition("/")
    if "@" in host:
        # ``https://user:pass@host`` would smuggle a credential into a field that is
        # shown in the form and read back on the auth verb.
        return _refuse(
            field,
            "it has a username or password in it",
            "paste only the address itself, with nothing before the @",
        )
    if not _HOST.match(host):
        return _refuse(
            field,
            "it does not name a website",
            "paste the address you see in your browser bar, like https://yourcompany.example.com",
        )
    trimmed = (slash + path).rstrip("/")
    assembled = f"{scheme}://{host}{trimmed}"
    if urlsplit(assembled).hostname is None:
        return _refuse(field, "it could not be read as an address", "check it and try again")
    return assembled


def _refuse(field: str, wrong: str, fix: str) -> str:
    """One refusal shape: what they typed it into, what is wrong, what to do.

    Never includes the value. A rejected address can hold a password, and this
    message is rendered in a browser, returned on an HTTP response, and logged.
    """
    raise ExtensionError(
        code=FIELD_FORMAT_INVALID,
        message=f"The value for '{field}' cannot be used because {wrong} — {fix}.",
        details={"field": field},
    )


__all__ = ["FIELD_FORMAT_INVALID", "SuppliedFormat", "normalize"]
