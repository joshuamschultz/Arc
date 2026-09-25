"""The pure half of a remote sign-in: what may reach argv, and who may be mid-flight.

Everything here is checked BEFORE a process starts. An address pasted out of a
browser bar is untrusted input that ends up as one argv value of a program holding
an OAuth client — so its shape is pinned exactly (loopback, the callback path, the
query keys a provider really sends, a bounded length) and anything else is refused
with a reason a person can act on, never with the value echoed back.
"""

from __future__ import annotations

import pytest

from arcagent.core.errors import ExtensionError
from arcagent.extension.remote_login import (
    REMOTE_LOGIN_BUSY,
    REMOTE_LOGIN_INVALID,
    REMOTE_LOGIN_NOT_STARTED,
    PendingLogin,
    RemoteLoginLedger,
    checked_account,
    checked_redirect_url,
    consent_link,
)

_STATE = "s7aTe-_x9"
_GOOD = f"http://127.0.0.1:40123/oauth2/callback?state={_STATE}&code=4/0Ab_cd-EF&scope=email%20https://www.googleapis.com/auth/gmail.modify&authuser=0&prompt=consent"
_CONSENT = (
    "https://accounts.google.com/o/oauth2/auth?client_id=abc.apps.googleusercontent.com"
    "&redirect_uri=http%3A%2F%2F127.0.0.1%3A40123%2Foauth2%2Fcallback"
    f"&response_type=code&state={_STATE}&access_type=offline"
)


# --- the account ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["josh@blackarcindustrial.com", "hello@joshuaschultz.com", "a.b+c_d-e@x-y.co.uk"]
)
def test_a_real_address_is_accepted(value: str) -> None:
    assert checked_account(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "--help@x.com",
        "-x@y.com",
        "a b@c.com",
        "a@b",
        "a@@b.com",
        "a;rm -rf /@x.com",
        "$(id)@x.com",
        "a@b.com\n--auth-url=x",
        "a@-b.com",
        "a" * 250 + "@x.com",
    ],
)
def test_anything_else_never_reaches_argv(value: str) -> None:
    with pytest.raises(ExtensionError) as refused:
        checked_account(value)
    assert refused.value.code == REMOTE_LOGIN_INVALID
    if value:
        assert value not in refused.value.message


# --- the pasted redirect ---------------------------------------------------------


def test_the_address_a_browser_lands_on_is_accepted() -> None:
    assert checked_redirect_url(_GOOD) == _GOOD


def test_surrounding_whitespace_from_a_paste_is_trimmed() -> None:
    assert checked_redirect_url(f"  {_GOOD}\n") == _GOOD


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "empty"),
        (_GOOD.replace("http://", "https://"), "http://127.0.0.1"),
        (_GOOD.replace("127.0.0.1", "evil.example"), "this computer"),
        (_GOOD.replace("127.0.0.1", "127.0.0.1.evil.example"), "this computer"),
        (_GOOD.replace("http://", "http://user:pw@"), "this computer"),
        (_GOOD.replace("/oauth2/callback", "/other"), "sign-in callback"),
        (_GOOD.replace("state=", "evil=1&state="), "unexpected"),
        (_GOOD + "&code=second", "more than once"),
        (_GOOD.replace(f"state={_STATE}&", ""), "state"),
        (_GOOD.replace("code=4/0Ab_cd-EF&", ""), "code"),
        (_GOOD + "#frag", "unexpected"),
        (_GOOD + " --force", "space"),
        (_GOOD.replace("code=", "code=a%0A"), "unexpected"),
        ("http://127.0.0.1:40123/oauth2/callback?error=access_denied&state=x", "declined"),
        (_GOOD + "&x=" + "a" * 5000, "too long"),
        ("javascript:alert(1)", "http://127.0.0.1"),
        ("--auth-url=http://127.0.0.1:1/oauth2/callback?code=a&state=b", "http://127.0.0.1"),
    ],
)
def test_a_wrong_address_is_refused_with_a_reason(value: str, reason: str) -> None:
    with pytest.raises(ExtensionError) as refused:
        checked_redirect_url(value)
    assert refused.value.code == REMOTE_LOGIN_INVALID
    assert reason in refused.value.message
    # The code is single-use and must not come back in anything an operator or a
    # log could read.
    assert "4/0Ab_cd-EF" not in refused.value.message


def test_it_must_come_back_to_the_port_and_state_this_sign_in_started() -> None:
    pending = PendingLogin(
        instance="work",
        account="a@b.com",
        redirect_base="http://127.0.0.1:40123/oauth2/callback",
        state=_STATE,
        started=0.0,
    )
    assert checked_redirect_url(_GOOD, expected=pending) == _GOOD
    with pytest.raises(ExtensionError, match="different sign-in"):
        checked_redirect_url(_GOOD.replace("40123", "40124"), expected=pending)
    with pytest.raises(ExtensionError, match="different sign-in"):
        checked_redirect_url(_GOOD.replace(_STATE, "other"), expected=pending)


# --- the consent link -------------------------------------------------------------


def test_the_consent_link_is_read_out_of_what_the_binary_printed() -> None:
    printed = f"auth_url\t{_CONSENT}\nstate_reused\tfalse\nRun again with ..."
    link = consent_link(printed, "accounts.google.com")
    assert link is not None
    assert link.url == _CONSENT
    assert link.redirect_base == "http://127.0.0.1:40123/oauth2/callback"
    assert link.state == _STATE


@pytest.mark.parametrize(
    "printed",
    [
        "no url here",
        _CONSENT.replace("accounts.google.com", "accounts.google.com.evil.example"),
        _CONSENT.replace("https://", "http://"),
        _CONSENT.replace("https://", "https://user@"),
        _CONSENT.replace("redirect_uri=http%3A%2F%2F127.0.0.1", "redirect_uri=http%3A%2F%2Fevil"),
        _CONSENT.replace(f"&state={_STATE}", ""),
    ],
)
def test_a_link_to_anywhere_else_is_not_handed_to_the_operator(printed: str) -> None:
    assert consent_link(printed, "accounts.google.com") is None


# --- one sign-in at a time ----------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _pending(account: str, clock: _Clock, instance: str = "work") -> PendingLogin:
    return PendingLogin(
        instance=instance,
        account=account,
        redirect_base="http://127.0.0.1:1/oauth2/callback",
        state="s",
        started=clock.now,
    )


def test_a_second_begin_for_another_account_is_refused_not_clobbered() -> None:
    clock = _Clock()
    ledger = RemoteLoginLedger(ttl=600, clock=clock)
    ledger.record("gog", _pending("a@b.com", clock))
    with pytest.raises(ExtensionError) as refused:
        ledger.admit_begin("gog", instance="other", account="c@d.com")
    assert refused.value.code == REMOTE_LOGIN_BUSY
    assert "a@b.com" in refused.value.message
    assert ledger.pending("gog") is not None


def test_restarting_the_same_account_is_allowed() -> None:
    clock = _Clock()
    ledger = RemoteLoginLedger(ttl=600, clock=clock)
    ledger.record("gog", _pending("a@b.com", clock))
    ledger.admit_begin("gog", instance="work", account="a@b.com")


def test_a_pending_sign_in_expires() -> None:
    clock = _Clock()
    ledger = RemoteLoginLedger(ttl=600, clock=clock)
    ledger.record("gog", _pending("a@b.com", clock))
    clock.now += 601
    assert ledger.pending("gog") is None
    ledger.admit_begin("gog", instance="other", account="c@d.com")


def test_complete_without_a_begin_is_refused() -> None:
    ledger = RemoteLoginLedger(ttl=600, clock=_Clock())
    with pytest.raises(ExtensionError) as refused:
        ledger.admit_complete("gog", instance="work", account="a@b.com")
    assert refused.value.code == REMOTE_LOGIN_NOT_STARTED


def test_complete_for_the_wrong_connection_is_refused() -> None:
    clock = _Clock()
    ledger = RemoteLoginLedger(ttl=600, clock=clock)
    ledger.record("gog", _pending("a@b.com", clock))
    with pytest.raises(ExtensionError) as refused:
        ledger.admit_complete("gog", instance="other", account="c@d.com")
    assert refused.value.code == REMOTE_LOGIN_NOT_STARTED


def test_complete_after_expiry_is_refused() -> None:
    clock = _Clock()
    ledger = RemoteLoginLedger(ttl=600, clock=clock)
    ledger.record("gog", _pending("a@b.com", clock))
    clock.now += 601
    with pytest.raises(ExtensionError, match="expired|start"):
        ledger.admit_complete("gog", instance="work", account="a@b.com")


def test_a_finished_sign_in_is_cleared() -> None:
    clock = _Clock()
    ledger = RemoteLoginLedger(ttl=600, clock=clock)
    ledger.record("gog", _pending("a@b.com", clock))
    ledger.clear("gog")
    assert ledger.pending("gog") is None
