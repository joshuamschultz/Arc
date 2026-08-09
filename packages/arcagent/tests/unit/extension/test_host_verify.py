"""``run_authorization_check`` — proving a sign-in happened, not that a binary runs.

The defect this exists for: the Authorise panel showed, in green with a tick,
**Signed in — dbxcli version: 3.7.1**, for a ``dbxcli`` that was not signed in at
all. The dropbox manifest declares ``probe_argv = ["version"]``, so the check
being rendered as "your account is connected" was really "does this program
start". Two different questions, two different answers, and the one being shown
sent the operator away from the only action that would have fixed it.

So a manifest now declares the command that proves authorisation, separately from
the probe, and these tests pin what makes that declaration trustworthy:

* **Exit code and output both count.** ``gh auth status`` exits non-zero when
  logged out; ``gog auth list`` exits zero and prints nothing useful, and
  ``ms-365-mcp-server --verify-login`` exits zero and prints
  ``{"success":false}``. A check that read only the exit code would call the last
  two signed in.
* **A bundle that declares no check reports unknown.** Never authorised — that is
  the lie — and never a silent "no", which would send an operator to a terminal
  for a sign-in that is already done.
* **The same three bounds as the login.** ``shlex.split`` + ``exec`` so a
  manifest cannot smuggle a second command, ``argv[0]`` must be the declared
  binary, and a command that does not end is killed.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest
from arctrust.audit import AuditEvent

from arcagent.core.tier import Tier
from arcagent.extension.host_login import (
    AuthorizationCheck,
    authorization_verdict,
    run_authorization_check,
)
from arcagent.extension.manifest import HostRequirement

_CALLER = "did:arc:test-caller"


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _requirement(script: str, *, pattern: str = "") -> HostRequirement:
    """A requirement whose "binary" is this interpreter running ``script``.

    A real subprocess, because what is under test is what the binary said and
    what was made of it — both invisible to a substitute.
    """
    return HostRequirement(
        name=sys.executable,
        authorize_command=f"{sys.executable} -c 'login'",
        verify_command=f"{sys.executable} -c {shlex.quote(script)}",
        verify_pattern=pattern,
    )


#: The shape ``gh auth status`` has: signed out is a non-zero exit.
_SIGNED_OUT_BY_EXIT = "import sys; sys.stderr.write('not logged in\\n'); sys.exit(1)"

#: The shape ``dbxcli account`` has when it works.
_SIGNED_IN = "print('joshua@blackarc.example')"

#: The shape ``gog auth list`` has when signed out: exit 0, and an empty listing.
_EMPTY_LISTING = "print('{\"accounts\":[]}')"

#: …and when signed in.
_FULL_LISTING = 'print(\'{"accounts":[{"email":"joshua@blackarc.example"}]}\')'


async def _check(
    requirement: HostRequirement, sink: _RecordingSink, timeout: float = 30.0
) -> AuthorizationCheck:
    return await run_authorization_check(
        requirement,
        caller_did=_CALLER,
        audit_sink=sink,
        tier=Tier.PERSONAL,
        timeout=timeout,
    )


# --- the two states are told apart -------------------------------------------


async def test_a_binary_that_is_signed_in_reports_authorized() -> None:
    result = await _check(_requirement(_SIGNED_IN), _RecordingSink())

    assert result.authorized
    assert "joshua@blackarc.example" in result.detail


async def test_a_binary_that_runs_but_is_signed_out_reports_not_authorized() -> None:
    """The whole defect: present and runnable is not the same as signed in."""
    result = await _check(_requirement(_SIGNED_OUT_BY_EXIT), _RecordingSink())

    assert not result.authorized
    assert "not logged in" in result.detail


async def test_a_check_that_exits_zero_while_signed_out_is_caught_by_its_pattern() -> None:
    """``gog auth list`` exits 0 with an empty listing. Exit code alone would lie."""
    requirement = _requirement(_EMPTY_LISTING, pattern='"email"')

    result = await _check(requirement, _RecordingSink())

    assert not result.authorized


async def test_the_same_pattern_reports_authorized_once_an_account_exists() -> None:
    requirement = _requirement(_FULL_LISTING, pattern='"email"')

    result = await _check(requirement, _RecordingSink())

    assert result.authorized


# --- a bundle with no declared check says so ----------------------------------


async def test_a_requirement_declaring_no_check_is_not_run_and_is_not_a_no() -> None:
    """Unknown, never "signed out": a bundle Arc cannot check is not a bundle that
    is logged out, and sending an operator to fix a sign-in that is already done is
    the same defect pointed the other way."""
    requirement = HostRequirement(name="acme", authorize_command="acme auth login")

    result = await _check(requirement, _RecordingSink())

    assert result.known is False
    assert result.authorized is False


# --- the manifest string cannot become arbitrary execution --------------------


async def test_a_check_naming_a_different_binary_is_refused_without_running(
    tmp_path: Path,
) -> None:
    """``argv[0]`` must be the declared binary; the field authorises one program."""
    marker = tmp_path / "ran"
    script = f"open({str(marker)!r}, 'w').close()"
    requirement = HostRequirement(
        name="acme", verify_command=f"{sys.executable} -c {shlex.quote(script)}"
    )

    result = await _check(requirement, _RecordingSink())

    assert not result.authorized
    assert not result.known
    assert not marker.exists()


async def test_a_shell_metacharacter_is_an_argument_not_a_second_command(
    tmp_path: Path,
) -> None:
    """A manifest is adversarial input: no shell parses the string it declares."""
    marker = tmp_path / "second"
    requirement = HostRequirement(
        name=sys.executable,
        verify_command=f"{sys.executable} -c 'print(1)' ; touch {marker}",
    )

    await _check(requirement, _RecordingSink())

    assert not marker.exists()


async def test_a_check_that_never_finishes_is_killed_rather_than_awaited() -> None:
    requirement = _requirement("import time; time.sleep(120)")

    result = await _check(requirement, _RecordingSink(), timeout=0.5)

    assert not result.authorized
    assert "did not finish" in result.detail


async def test_a_binary_that_is_not_on_this_host_is_a_verdict_not_a_crash() -> None:
    requirement = HostRequirement(
        name="definitely_not_installed_xyz",
        verify_command="definitely_not_installed_xyz status",
    )

    result = await _check(requirement, _RecordingSink())

    assert not result.authorized


# --- the verdict is on the record either way ---------------------------------


async def test_both_outcomes_are_audited_naming_the_binary() -> None:
    sink = _RecordingSink()

    await _check(_requirement(_SIGNED_IN), sink)
    await _check(_requirement(_SIGNED_OUT_BY_EXIT), sink)

    assert [event.outcome for event in sink.events] == ["allow", "deny"]
    assert all(event.actor_did == _CALLER for event in sink.events)
    assert all(event.extra["binary"] == sys.executable for event in sink.events)


# --- the verdict itself, over recorded output --------------------------------


@pytest.mark.parametrize(
    "pattern,returncode,output,expected",
    [
        ("", 0, "anything", True),
        ("", 1, "anything", False),
        ('"email"', 0, '{"accounts":[]}', False),
        ('"email"', 0, '{"accounts":[{"email":"a@b.c"}]}', True),
        ('"success"\\s*:\\s*true', 0, '{"success":false}', False),
        ('"success"\\s*:\\s*true', 0, '{"success":true}', True),
        ('"email"', 1, '{"accounts":[{"email":"a@b.c"}]}', False),
    ],
)
def test_the_verdict_reads_the_exit_code_and_the_output_together(
    pattern: str, returncode: int, output: str, expected: bool
) -> None:
    requirement = HostRequirement(
        name="acme", verify_command="acme status", verify_pattern=pattern
    )

    assert authorization_verdict(requirement, returncode, output) is expected


async def test_a_command_no_shell_grammar_parses_is_refused_rather_than_raising() -> None:
    """An unbalanced quote in a manifest is a refusal, not a 500 in the panel."""
    requirement = HostRequirement(name="acme", verify_command='acme auth "list')

    result = await _check(requirement, _RecordingSink())

    assert not result.known
    assert not result.authorized
