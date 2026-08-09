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
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from arcagent.core.errors import ExtensionError

#: The shapes core knows how to enforce. An empty format is "whatever the operator
#: typed, exactly" — the right answer for a token, whose leading and trailing
#: characters may be significant and which normalising would corrupt into a 401
#: nobody can explain. A closed set on purpose: a shape nothing enforces is a
#: control an operator believes is in force, so an unknown one is a manifest error.
SuppliedFormat = Literal["", "https_url"]

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
    return value


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
