"""``run_remote_login_begin`` / ``run_remote_login_complete`` — the two steps, bounded.

The same three bounds every manifest-declared command takes (no shell, ``argv[0]``
is the declared binary, a hard timeout), plus what is particular to this flow:

* the begin hands back a consent link ONLY when it points at the declared host;
* the pasted address is checked before anything runs, and its authorization code
  never appears in a returned line, a log record, or an audit event;
* a step whose inputs fail a check is refused AND audited — an attempt nobody can
  see is the one an attacker repeats.

The "binary" is this interpreter running a small script, so each assertion is
about a real child process.
"""

from __future__ import annotations

import logging
import shlex
import sys
from pathlib import Path

import pytest
from arctrust.audit import AuditEvent

from arcagent.core.tier import Tier
from arcagent.extension.host_login import (
    authorization_verdict,
    expired_verdict,
    run_authorization_check,
    run_remote_login_begin,
    run_remote_login_complete,
)
from arcagent.extension.manifest import HostRequirement, RemoteLogin
from arcagent.extension.remote_login import PendingLogin

_CALLER = "did:arc:test-caller"
_CODE = "4/zzz-single-use-code-5521"
_STATE = "st4te"
_REDIRECT = f"http://127.0.0.1:40123/oauth2/callback?state={_STATE}&code={_CODE}&scope=email"
_CONSENT = (
    "https://accounts.google.com/o/oauth2/auth?client_id=abc"
    "&redirect_uri=http%3A%2F%2F127.0.0.1%3A40123%2Foauth2%2Fcallback"
    f"&state={_STATE}&response_type=code"
)
_PENDING = PendingLogin(
    instance="work",
    account="a@b.com",
    redirect_base="http://127.0.0.1:40123/oauth2/callback",
    state=_STATE,
    started=0.0,
)


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake.py"
    path.write_text("import sys, os, time\n" + body, encoding="utf-8")
    return path


def _requirement(script: Path, *, expired: str = "") -> HostRequirement:
    run = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    return HostRequirement(
        name=sys.executable,
        authorize_command="x",
        verify_command=f"{run} verify",
        verify_pattern="OK",
        verify_expired_pattern=expired,
        remote_login=RemoteLogin(
            begin=f"{run} begin {{account}}",
            complete=f"{run} complete {{account}} --auth-url {{redirect_url}}",
            consent_host="accounts.google.com",
        ),
    )


_PRINTS_LINK = f"""
if sys.argv[1] == 'begin':
    print('auth_url\\t{_CONSENT}')
    print('state_reused\\tfalse')
elif sys.argv[1] == 'complete':
    assert sys.argv[2] == 'a@b.com' and sys.argv[3] == '--auth-url'
    print('stored token for', sys.argv[2], 'from', sys.argv[4])
"""


async def _begin(requirement: HostRequirement, sink: _RecordingSink, **values: str) -> object:
    return await run_remote_login_begin(
        requirement,
        values=values or {"account": "a@b.com"},
        caller_did=_CALLER,
        audit_sink=sink,
        tier=Tier.PERSONAL,
        instance="work",
    )


async def _complete(
    requirement: HostRequirement,
    sink: _RecordingSink,
    url: str = _REDIRECT,
    *,
    timeout: float = 30.0,
) -> object:
    return await run_remote_login_complete(
        requirement,
        values={"account": "a@b.com"},
        redirect_url=url,
        expected=_PENDING,
        caller_did=_CALLER,
        audit_sink=sink,
        tier=Tier.PERSONAL,
        instance="work",
        timeout=timeout,
    )


async def test_begin_hands_back_the_consent_link(tmp_path: Path) -> None:
    sink = _RecordingSink()
    step = await _begin(_requirement(_script(tmp_path, _PRINTS_LINK)), sink)
    assert step.completed  # type: ignore[attr-defined]
    assert step.link.url == _CONSENT  # type: ignore[attr-defined]
    assert [event.action for event in sink.events] == ["extension.host.remote_login.begin"]
    assert sink.events[0].outcome == "allow"


async def test_begin_refuses_a_link_to_another_host(tmp_path: Path) -> None:
    body = _PRINTS_LINK.replace("accounts.google.com", "accounts.evil.example")
    sink = _RecordingSink()
    step = await _begin(_requirement(_script(tmp_path, body)), sink)
    assert not step.completed  # type: ignore[attr-defined]
    assert step.link is None  # type: ignore[attr-defined]
    assert "accounts.evil.example" not in step.detail  # type: ignore[attr-defined]
    assert sink.events[0].outcome == "deny"


async def test_begin_refuses_a_missing_account_without_running(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    body = f"open({str(marker)!r}, 'w').write('x')\n"
    sink = _RecordingSink()
    step = await run_remote_login_begin(
        _requirement(_script(tmp_path, body)),
        values={},
        caller_did=_CALLER,
        audit_sink=sink,
        tier=Tier.PERSONAL,
        instance="work",
    )
    assert not step.completed
    assert "account" in step.detail
    assert not marker.exists()
    assert sink.events[0].outcome == "deny"


@pytest.mark.parametrize("value", ["--help", "a b", "x\n--auth-url=evil", ""])
async def test_a_value_that_could_become_a_flag_never_runs(tmp_path: Path, value: str) -> None:
    marker = tmp_path / "ran"
    body = f"open({str(marker)!r}, 'w').write('x')\n"
    sink = _RecordingSink()
    step = await _begin(_requirement(_script(tmp_path, body)), sink, account=value or " ")
    assert not step.completed  # type: ignore[attr-defined]
    assert not marker.exists()


async def test_complete_passes_the_address_as_one_argument_and_never_echoes_the_code(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sink = _RecordingSink()
    with caplog.at_level(logging.DEBUG):
        step = await _complete(_requirement(_script(tmp_path, _PRINTS_LINK)), sink)
    assert step.completed  # type: ignore[attr-defined]
    assert _CODE not in step.detail  # type: ignore[attr-defined]
    assert _CODE not in caplog.text
    assert all(_CODE not in repr(event) for event in sink.events)
    assert [event.action for event in sink.events] == ["extension.host.remote_login.complete"]


async def test_complete_refuses_a_bad_address_before_anything_runs(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    body = f"open({str(marker)!r}, 'w').write('x')\n"
    sink = _RecordingSink()
    step = await _complete(
        _requirement(_script(tmp_path, body)),
        sink,
        url=_REDIRECT.replace("127.0.0.1", "evil.example"),
    )
    assert not step.completed  # type: ignore[attr-defined]
    assert not marker.exists()
    assert sink.events[0].outcome == "deny"
    assert sink.events[0].extra.get("reason") == "invalid_input"


async def test_complete_reports_the_binarys_refusal_redacted(tmp_path: Path) -> None:
    body = "sys.stderr.write('exchange failed for ' + sys.argv[4] + '\\n'); sys.exit(1)\n"
    sink = _RecordingSink()
    step = await _complete(_requirement(_script(tmp_path, body)), sink)
    assert not step.completed  # type: ignore[attr-defined]
    assert "exchange failed" in step.detail  # type: ignore[attr-defined]
    assert _CODE not in step.detail  # type: ignore[attr-defined]
    assert sink.events[0].outcome == "deny"


async def test_a_step_that_does_not_end_is_killed(tmp_path: Path) -> None:
    body = "time.sleep(30)\n"
    sink = _RecordingSink()
    step = await _complete(_requirement(_script(tmp_path, body)), sink, timeout=0.5)
    assert not step.completed  # type: ignore[attr-defined]
    assert "did not finish" in step.detail  # type: ignore[attr-defined]


# --- reading an expired credential apart from an absent one ---------------------


_EXPIRED = 'refresh access token: oauth2: "invalid_grant" "Bad Request"'


def test_an_expired_credential_is_told_apart_from_none() -> None:
    requirement = HostRequirement(
        name="gog",
        verify_command="gog x",
        verify_pattern="OK",
        verify_expired_pattern="invalid_grant",
    )
    assert not authorization_verdict(requirement, 1, _EXPIRED)
    assert expired_verdict(requirement, 1, _EXPIRED)
    assert not expired_verdict(requirement, 1, "No auth for gmail a@b.com")
    # A success is never "expired", whatever it prints.
    assert not expired_verdict(requirement, 0, f"OK {_EXPIRED}")


async def test_the_check_reports_expired(tmp_path: Path) -> None:
    body = f"sys.stderr.write({_EXPIRED!r}); sys.exit(1)\n"
    requirement = _requirement(_script(tmp_path, body), expired="invalid_grant")
    check = await run_authorization_check(
        requirement, caller_did=_CALLER, audit_sink=_RecordingSink(), tier=Tier.PERSONAL
    )
    assert check.known and not check.authorized and check.expired
