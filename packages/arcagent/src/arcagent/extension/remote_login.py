"""The checks a remote sign-in passes before any process starts, and who is mid-flight.

A remote login (:class:`~arcagent.extension.manifest.RemoteLogin`) is the headless
OAuth shape: step one prints a consent link, the operator signs in with the
provider in their own browser and lands on a loopback address that fails to load,
and step two exchanges the address they paste. Arc drives both steps from a web
page; this module is everything about that which is decided without running
anything, so :mod:`arcagent.extension.host_login` can refuse before it spawns.

**The authorization code crosses on argv, and why that is acceptable here.** The
pasted address carries a one-time authorization code, and the binaries that have
this flow (``gog auth add --remote --step 2 --auth-url``) accept it only as an
argument — there is no stdin form. Argv is the process table, readable by other
users on the host, which is why a TOKEN never goes there (see ``token_command``).
A code is a different thing:

* it is single-use: the exchange that follows within milliseconds consumes it,
  and a second redemption is refused by the provider;
* it is PKCE-bound (RFC 7636): redeeming it requires the ``code_verifier`` that
  step one generated and stored on this host, in the binary's own state directory,
  and that verifier never leaves the host — not on argv, not in a response, not in
  a log. Someone who reads the code off the process table without the verifier
  holds nothing redeemable, and one who can read the binary's state directory
  already holds the refresh tokens it protects;
* it is short-lived (minutes) and names this OAuth client only.

So the exposure is a short window over a value that is useless alone. Arc still
keeps it out of everything Arc writes: it appears in no audit event, no log
record, no refusal message and no response body, and output from the binary is
redacted of it before an operator reads a line.

**One sign-in at a time per binary.** The binary's stored step-one state is not
keyed by account — ``gog`` reuses the newest matching state for the next step one
— so two operators starting two accounts at once would silently hand one of them
the other's consent link. :class:`RemoteLoginLedger` refuses the second begin by
name instead, lets the same account restart, and forgets a begin after a bounded
time shorter than the binary's own state lifetime.
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from arcagent.core.errors import ExtensionError

#: A pasted address that fails a check. The message says what to do; it never
#: repeats what was pasted, because what was pasted is a live authorization code.
REMOTE_LOGIN_INVALID = "REMOTE_LOGIN_INVALID"

#: A begin while another account's sign-in is still waiting.
REMOTE_LOGIN_BUSY = "REMOTE_LOGIN_BUSY"

#: A complete with no begin for this connection, or one that has expired.
REMOTE_LOGIN_NOT_STARTED = "REMOTE_LOGIN_NOT_STARTED"

#: A step the binary itself refused, or that did not finish.
REMOTE_LOGIN_FAILED = "REMOTE_LOGIN_FAILED"

#: The longest address accepted. A real callback is a few hundred characters; the
#: bound is what stops a paste from becoming an unbounded argv value (LLM10).
_MAX_REDIRECT_LENGTH = 2048

#: The query keys a provider's loopback callback carries. Google sends ``code``,
#: ``state``, ``scope``, ``authuser``, ``prompt``, and ``hd`` for Workspace domains,
#: and ``iss`` under RFC 9207. Anything else was not put there by the provider.
_CALLBACK_KEYS = frozenset({"code", "state", "scope", "authuser", "hd", "prompt", "iss"})

#: Where a loopback callback may point: this computer, and only this computer.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_CALLBACK_PATH = "/oauth2/callback"

#: Strict on purpose — this string becomes an argv value. Letters, digits and the
#: punctuation real mailboxes use, one ``@``, and a dotted domain whose labels
#: neither start nor end with a hyphen. Nothing that begins with ``-``.
_ACCOUNT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@"
    r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

_MAX_ACCOUNT_LENGTH = 254

#: An absolute https link inside whatever the binary printed.
_LINK = re.compile(r"https://[^\s\"'<>]+")

#: A step-one state value, as the binaries that mint one spell it.
_STATE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def _invalid(reason: str) -> ExtensionError:
    return ExtensionError(code=REMOTE_LOGIN_INVALID, message=reason, details={})


def checked_account(value: str) -> str:
    """The account address, or a refusal before it can reach argv.

    Raises:
        ExtensionError: ``code`` :data:`REMOTE_LOGIN_INVALID`. The message never
            echoes the value.
    """
    if len(value) > _MAX_ACCOUNT_LENGTH or _ACCOUNT.fullmatch(value) is None:
        raise _invalid(
            "this connection's account is not a plain email address — set it to the "
            "address you sign in with, like you@yourcompany.com, then try again"
        )
    return value


#: The longest field value a sign-in step will put on argv.
_MAX_ARGV_VALUE = 254


def checked_argv_value(field: str, value: str) -> str:
    """A bundle field's value, safe to be exactly one argv value of a sign-in step.

    Every step is split BEFORE its fields are filled, so a value can never become
    an argument of its own — but a value starting with ``-`` is still read as a
    flag by the binary, and whitespace or a control character in an address is
    never part of it. Refused, named, never echoed.

    Raises:
        ExtensionError: ``code`` :data:`REMOTE_LOGIN_INVALID`.
    """
    if (
        not value
        or len(value) > _MAX_ARGV_VALUE
        or value.startswith("-")
        or any(character.isspace() or _unprintable(character) for character in value)
    ):
        raise _invalid(
            f"this connection's {field} is empty or has characters a sign-in cannot use — "
            f"set it again, then try again"
        )
    return value


@dataclass(frozen=True)
class PendingLogin:
    """One begun sign-in: which connection, and where its callback must come back to.

    ``state`` is the CSRF value from the consent link. It is held only in this
    process's memory and compared against the pasted address, so an address from
    a different or older sign-in is refused before the binary sees it.
    """

    instance: str
    account: str
    redirect_base: str
    state: str
    started: float


def checked_redirect_url(value: str, *, expected: PendingLogin | None = None) -> str:
    """The pasted callback address, trimmed, or a refusal naming what is wrong.

    Args:
        value: What the operator pasted.
        expected: The begun sign-in it must answer. When given, the address must
            come back to the same loopback port and carry the same ``state``.

    Raises:
        ExtensionError: ``code`` :data:`REMOTE_LOGIN_INVALID`.
    """
    pasted = value.strip()
    if not pasted:
        raise _invalid("the address is empty — paste the whole address from the browser bar")
    if len(pasted) > _MAX_REDIRECT_LENGTH:
        raise _invalid("that address is too long to be a sign-in callback — copy it again")
    if any(character.isspace() for character in pasted):
        raise _invalid(
            "the address has a space or line break inside it — copy only the address "
            "from the browser bar"
        )
    if not pasted.lower().startswith("http://"):
        raise _invalid(
            "paste the address the browser landed on after you signed in — it starts "
            "with http://127.0.0.1"
        )
    parts = urlsplit(pasted)
    host = (parts.hostname or "").lower()
    if parts.username is not None or parts.password is not None or host not in _LOOPBACK_HOSTS:
        raise _invalid(
            "that address does not point at this computer — paste the one that starts "
            "with http://127.0.0.1"
        )
    if parts.path != _CALLBACK_PATH:
        raise _invalid("that is not the sign-in callback address — it ends in /oauth2/callback")
    query = _callback_query(parts.query, parts.fragment)
    if "error" in query:
        raise _invalid(
            "Google reports the sign-in was declined or cancelled — start again and choose Allow"
        )
    for required in ("code", "state"):
        if not query.get(required):
            raise _invalid(
                f"the address has no {required} in it — make sure you copied the whole "
                f"address, including everything after the ?"
            )
    if expected is not None and (
        f"{parts.scheme}://{parts.netloc}{parts.path}" != expected.redirect_base
        or query["state"] != expected.state
    ):
        raise _invalid(
            "that address is from a different sign-in — use the address from the "
            "Google page you opened last, or start again"
        )
    return pasted


def _callback_query(query: str, fragment: str) -> dict[str, str]:
    """The callback's query keys, refused if one is unknown, repeated or unprintable."""
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise _invalid("that address is damaged — copy it again from the browser bar") from exc
    names = [name for name, _ in pairs]
    if fragment or not set(names) <= _CALLBACK_KEYS | {"error"}:
        raise _invalid(
            "that address has unexpected parts in it — copy it again from the browser bar"
        )
    if len(names) != len(set(names)):
        raise _invalid("that address names the same part more than once — copy it again")
    if any(_unprintable(text) for pair in pairs for text in pair):
        raise _invalid("that address has unexpected characters in it — copy it again")
    return dict(pairs)


def _unprintable(text: str) -> bool:
    """True when ``text`` holds a control, format, or separator character other than a space."""
    return any(
        unicodedata.category(character).startswith(("C", "Z"))
        for character in text
        if character != " "
    )


@dataclass(frozen=True)
class ConsentLink:
    """The link to hand the operator, and what its callback must match."""

    url: str
    redirect_base: str
    state: str


def consent_link(printed: str, consent_host: str) -> ConsentLink | None:
    """The first link in ``printed`` that points at ``consent_host`` and back here.

    A link anywhere else is never handed to an operator: the whole flow rests on
    them signing in at the provider, and a binary (or a compromised one) printing
    a lookalike host would otherwise turn Arc into a phishing relay. The link must
    also name a loopback ``redirect_uri`` and a ``state``, because those are what
    the pasted address is checked against.
    """
    for candidate in _LINK.findall(printed):
        parts = urlsplit(candidate)
        if parts.username is not None or (parts.hostname or "").lower() != consent_host:
            continue
        query = dict(parse_qsl(parts.query))
        redirect = urlsplit(query.get("redirect_uri", ""))
        state = query.get("state", "")
        if (
            redirect.scheme == "http"
            and (redirect.hostname or "").lower() in _LOOPBACK_HOSTS
            and redirect.path == _CALLBACK_PATH
            and _STATE.fullmatch(state)
        ):
            base = f"{redirect.scheme}://{redirect.netloc}{redirect.path}"
            return ConsentLink(url=candidate, redirect_base=base, state=state)
    return None


class RemoteLoginLedger:
    """Which remote sign-in each host binary is waiting on — one at a time, bounded.

    In memory and owned by the long-lived caller (the web server), because a begun
    sign-in lives exactly as long as the process that can complete it. Nothing in
    it is a credential: the ``state`` value is a CSRF nonce already shown in the
    consent link. A restart forgets it, and the operator starts again — which is
    the fail-closed answer, not a lost sign-in.

    ``lock`` serialises begin and complete per binary, so two requests racing for
    the same step cannot both pass the checks below.
    """

    def __init__(self, *, ttl: float = 540.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._pending: dict[str, PendingLogin] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def ttl(self) -> float:
        """Seconds a begun sign-in stays completable."""
        return self._ttl

    def lock(self, binary: str) -> asyncio.Lock:
        """The one lock both steps of every sign-in through ``binary`` take."""
        return self._locks.setdefault(binary, asyncio.Lock())

    def pending(self, binary: str) -> PendingLogin | None:
        """The live begun sign-in for ``binary``, forgetting one that has expired."""
        found = self._pending.get(binary)
        if found is not None and self._clock() - found.started > self._ttl:
            del self._pending[binary]
            return None
        return found

    def admit_begin(self, binary: str, *, instance: str, account: str) -> None:
        """Refuse a begin that would clobber another account's waiting sign-in.

        Restarting the SAME connection's sign-in is allowed and replaces it: that
        is an operator who closed the tab, and making them wait out the timer
        would be a wall with no reason behind it.
        """
        waiting = self.pending(binary)
        if waiting is None or (waiting.instance, waiting.account) == (instance, account):
            return
        remaining = max(0, int(self._ttl - (self._clock() - waiting.started)))
        raise ExtensionError(
            code=REMOTE_LOGIN_BUSY,
            message=(
                f"a sign-in for {waiting.account} (connection {waiting.instance!r}) is still "
                f"waiting — finish it first, or try again in {remaining // 60 + 1} min"
            ),
            details={"instance": waiting.instance},
        )

    def admit_complete(self, binary: str, *, instance: str, account: str) -> PendingLogin:
        """The begun sign-in this complete answers, or a refusal saying to start again."""
        waiting = self.pending(binary)
        if waiting is None or (waiting.instance, waiting.account) != (instance, account):
            raise ExtensionError(
                code=REMOTE_LOGIN_NOT_STARTED,
                message=(
                    f"no sign-in is waiting for {instance!r} — it expired or was never "
                    f"started. Click Open Google sign-in to start again."
                ),
                details={"instance": instance},
            )
        return waiting

    def record(self, binary: str, pending: PendingLogin) -> None:
        """Remember a begun sign-in, replacing any earlier one for the same connection."""
        self._pending[binary] = pending

    def clear(self, binary: str) -> None:
        """Forget the begun sign-in: it was completed, or its code was spent trying."""
        self._pending.pop(binary, None)

    def now(self) -> float:
        """The ledger's clock, so a begin is stamped on the same scale it expires on."""
        return self._clock()


__all__ = [
    "REMOTE_LOGIN_BUSY",
    "REMOTE_LOGIN_FAILED",
    "REMOTE_LOGIN_INVALID",
    "REMOTE_LOGIN_NOT_STARTED",
    "ConsentLink",
    "PendingLogin",
    "RemoteLoginLedger",
    "checked_account",
    "checked_argv_value",
    "checked_redirect_url",
    "consent_link",
]
