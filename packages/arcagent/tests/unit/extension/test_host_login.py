"""SPEC-064 — ``run_token_login``: the one sign-in Arc can finish, and no other.

Most connector logins cannot be completed by a button. ``gog auth add`` opens a
browser, ``ms-365-mcp-server --login`` prints a device code and waits for a
person. Only a binary that reads a token on stdin and exits is finishable
headlessly, and a manifest says which kind it has. These tests pin the three
properties that keep the distinction from becoming a lie or a hole:

* **The token crosses on stdin and nowhere else.** Never on argv — argv is in the
  process table for every other user on the box — and never in a returned string,
  a log record, or an audit event. Asserted with a sentinel against every one of
  those surfaces, including output the binary itself echoes back.
* **The command is argv, not a shell line.** A manifest is adversarial input;
  ``shlex.split`` plus ``create_subprocess_exec`` means a ``;`` is an argument
  rather than a second command, and argv[0] must be the very binary the
  requirement declares.
* **A login that does not end is killed.** An interactive command declared as a
  token command would otherwise hang the request forever.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest
from arctrust.audit import AuditEvent

from arcagent.core.tier import Tier
from arcagent.extension.host_login import run_token_login
from arcagent.extension.manifest import HostRequirement

_CALLER = "did:arc:test-caller"

#: Distinctive enough that finding it anywhere is proof of a leak.
_SENTINEL = "zzz-token-sentinel-8812"


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _requirement(script: str) -> HostRequirement:
    """A requirement whose "binary" is this interpreter running ``script``.

    A real subprocess rather than a substitute: the properties under test are
    where the token goes and what argv becomes, and both are invisible to a fake.
    """
    return HostRequirement(
        name=sys.executable,
        authorize_command=f"{sys.executable} -c 'interactive'",
        token_command=f"{sys.executable} -c {script!r}",
    )


#: Reads the token from stdin, proves it arrived, and exits 0.
_ACCEPTS = (
    "import sys; token = sys.stdin.read().strip();"
    " sys.exit(0 if token else 1);"
)

#: Echoes what it was given — the binary that would leak the token for us.
_ECHOES = "import sys; print('signed in as', sys.stdin.read().strip())"

#: Exits non-zero: the login the operator's token did not satisfy.
_REFUSES = "import sys; sys.stderr.write('bad token\\n'); sys.exit(1)"


async def _login(requirement: HostRequirement, token: str, sink: _RecordingSink) -> object:
    return await run_token_login(
        requirement, token=token, caller_did=_CALLER, audit_sink=sink, tier=Tier.PERSONAL
    )


# --- the login runs, and the token reaches it --------------------------------


async def test_a_token_login_completes_and_the_token_arrives_on_stdin() -> None:
    """The binary exits 0 only when it actually read a token."""
    result = await _login(_requirement(_ACCEPTS), _SENTINEL, _RecordingSink())

    assert result.completed


async def test_an_empty_token_does_not_satisfy_the_login() -> None:
    result = await _login(_requirement(_ACCEPTS), "", _RecordingSink())

    assert not result.completed


async def test_a_login_the_binary_rejects_is_reported_as_failed_not_as_success() -> None:
    """A button that reports success on a rejected token is worse than no button."""
    result = await _login(_requirement(_REFUSES), _SENTINEL, _RecordingSink())

    assert not result.completed
    assert "bad token" in result.detail


# --- the token never comes back out ------------------------------------------


async def test_the_token_is_absent_from_the_result_even_when_the_binary_echoes_it() -> None:
    """The one surface Arc cannot control is what the binary prints. So it is redacted."""
    result = await _login(_requirement(_ECHOES), _SENTINEL, _RecordingSink())

    assert _SENTINEL not in result.detail
    assert "signed in as" in result.detail


async def test_the_token_is_absent_from_every_audit_event() -> None:
    """An audit chain is read by people who are not the operator who typed it."""
    sink = _RecordingSink()

    await _login(_requirement(_ECHOES), _SENTINEL, sink)

    assert sink.events
    assert _SENTINEL not in repr([event.__dict__ for event in sink.events])


async def test_the_token_is_absent_from_the_log(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("DEBUG"):
        await _login(_requirement(_ECHOES), _SENTINEL, _RecordingSink())

    assert _SENTINEL not in caplog.text


async def test_the_token_never_reaches_argv(tmp_path: Path) -> None:
    """argv is world-readable in the process table; stdin is not.

    The binary writes its own argv out, so this asserts on what the process
    really received rather than on how the call site looks.
    """
    recorded = tmp_path / "argv.txt"
    script = (
        f"import sys; open({str(recorded)!r}, 'w').write(repr(sys.argv)); sys.stdin.read()"
    )

    await _login(_requirement(script), _SENTINEL, _RecordingSink())

    assert _SENTINEL not in recorded.read_text()


# --- the command is argv, never a shell line ---------------------------------


async def test_a_command_naming_a_different_binary_is_refused_without_running(
    tmp_path: Path,
) -> None:
    """The declared prerequisite is the only thing a token command may invoke.

    Without this, ``token_command`` is a manifest field that runs any program on
    the host — the hole ``arcagent.extension.host`` refuses to open by never
    running ``instruction`` at all.
    """
    marker = tmp_path / "ran"
    script = shlex.quote(f"open({str(marker)!r}, 'w').write('ran')")
    requirement = HostRequirement(name="acme", token_command=f"{sys.executable} -c {script}")

    result = await _login(requirement, _SENTINEL, _RecordingSink())

    assert not result.completed
    assert not marker.exists()


async def test_a_shell_metacharacter_is_an_argument_not_a_second_command(
    tmp_path: Path,
) -> None:
    """No shell is involved, so ``;`` has no meaning to anything."""
    marker = tmp_path / "pwned"
    requirement = HostRequirement(name="acme", token_command=f"acme login; touch {marker}")

    result = await _login(requirement, _SENTINEL, _RecordingSink())

    assert not result.completed
    assert not marker.exists()


async def test_a_requirement_with_no_token_command_is_refused_without_running() -> None:
    """An interactive-only login must never be attempted — it would hang, then lie."""
    requirement = HostRequirement(name="acme", authorize_command="acme auth login")

    result = await _login(requirement, _SENTINEL, _RecordingSink())

    assert not result.completed
    assert "acme" in result.detail


# --- a login that does not end is ended ---------------------------------------


async def test_a_login_that_never_finishes_is_killed_rather_than_awaited() -> None:
    """A command mis-declared as non-interactive would otherwise hang the caller."""
    requirement = _requirement("import time; time.sleep(120)")

    result = await run_token_login(
        requirement,
        token=_SENTINEL,
        caller_did=_CALLER,
        audit_sink=_RecordingSink(),
        tier=Tier.PERSONAL,
        timeout=0.5,
    )

    assert not result.completed
    assert "did not finish" in result.detail


# --- the verdict is on the record either way ---------------------------------


async def test_both_outcomes_are_audited_naming_the_binary() -> None:
    sink = _RecordingSink()

    await _login(_requirement(_ACCEPTS), _SENTINEL, sink)
    await _login(_requirement(_REFUSES), _SENTINEL, sink)

    assert [event.outcome for event in sink.events] == ["allow", "deny"]
    assert all(event.actor_did == _CALLER for event in sink.events)
    assert all(event.extra["binary"] == sys.executable for event in sink.events)
