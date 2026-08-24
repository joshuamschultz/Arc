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

import ast
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
_ACCEPTS = "import sys; token = sys.stdin.read().strip(); sys.exit(0 if token else 1);"

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
    script = f"import sys; open({str(recorded)!r}, 'w').write(repr(sys.argv)); sys.stdin.read()"

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


# --- a login that needs more than a token -------------------------------------
#
# `gh auth login --with-token` needs only the token. Atlassian's is the other
# shape, and it is the common one: `acli jira auth login --site <site> --email
# <email> --token` needs the site and the address before the token means anything
# (measured: omitting one gives "if any flags in the group [token email site] are
# set they must all be set; missing [site]"). A `token_command` that could carry
# only the token could not sign that connector in at all, so the operator would be
# sent to a terminal for a login the button was built to finish.
#
# So the command may name the bundle's own declared fields, and they are filled in
# from what the operator already supplied. Two bounds make that safe, and both are
# asserted below: substitution happens AFTER the command is split, so no value can
# become a second argv token; and only NON-SENSITIVE fields may be named, so the
# rule that a credential never reaches argv is not weakened by this seam.


def _argv_recording_requirement(script_path: Path, command_tail: str) -> HostRequirement:
    """A requirement whose login writes its own argv out, then reads the token."""
    script = f"import sys; open({str(script_path)!r}, 'w').write(repr(sys.argv)); sys.stdin.read()"
    return HostRequirement(
        name=sys.executable,
        authorize_command=f"{sys.executable} -c 'interactive'",
        token_command=f"{sys.executable} -c {script!r} {command_tail}",
    )


async def test_a_declared_field_is_filled_into_the_login_argv(tmp_path: Path) -> None:
    """The value the operator supplied reaches the command that needs it."""
    recorded = tmp_path / "argv.txt"
    requirement = _argv_recording_requirement(recorded, "--site={site} --email={email}")

    result = await run_token_login(
        requirement,
        token=_SENTINEL,
        caller_did=_CALLER,
        audit_sink=_RecordingSink(),
        tier=Tier.PERSONAL,
        values={"site": "ctgfederal.atlassian.net", "email": "jschultz@ctgfederal.com"},
    )

    assert result.completed
    argv = recorded.read_text()
    assert "--site=ctgfederal.atlassian.net" in argv
    assert "--email=jschultz@ctgfederal.com" in argv


async def test_a_filled_value_can_never_become_a_second_argv_token(tmp_path: Path) -> None:
    """Substitution is after the split, so spaces and flags inside a value are inert."""
    recorded = tmp_path / "argv.txt"
    requirement = _argv_recording_requirement(recorded, "--site={site}")

    await run_token_login(
        requirement,
        token=_SENTINEL,
        caller_did=_CALLER,
        audit_sink=_RecordingSink(),
        tier=Tier.PERSONAL,
        values={"site": "a b --token /etc/passwd"},
    )

    argv = ast.literal_eval(recorded.read_text())
    assert argv[-1] == "--site=a b --token /etc/passwd"


@pytest.mark.parametrize(
    "value",
    ["one; touch /tmp/pwned", "a b", "--token", "$(whoami)", "`id`", "a\nb"],
)
async def test_no_filled_value_can_add_a_token_or_a_command(tmp_path: Path, value: str) -> None:
    """One declared field, one argv token, whatever the operator typed into it.

    Parametrised over the shapes that would break a shell, because nothing here is
    a shell: the value is substituted into a token that ``shlex.split`` already
    produced, so there is no second parse for a ``;`` to be meaningful to.
    """
    recorded = tmp_path / "argv.txt"
    requirement = _argv_recording_requirement(recorded, "--site={site}")

    await run_token_login(
        requirement,
        token=_SENTINEL,
        caller_did=_CALLER,
        audit_sink=_RecordingSink(),
        tier=Tier.PERSONAL,
        values={"site": value},
    )

    # The child sees ``["-c", "--site=…"]`` — python drops the script text from
    # argv — so two tokens is the whole command line, and a third would be a value
    # that had split itself in two.
    argv = ast.literal_eval(recorded.read_text())
    assert argv == ["-c", f"--site={value}"]


async def test_a_value_carrying_a_placeholder_is_not_expanded_again(tmp_path: Path) -> None:
    """One pass, so a supplied value cannot reach a field it was not given."""
    recorded = tmp_path / "argv.txt"
    requirement = _argv_recording_requirement(recorded, "--site={site}")

    await run_token_login(
        requirement,
        token=_SENTINEL,
        caller_did=_CALLER,
        audit_sink=_RecordingSink(),
        tier=Tier.PERSONAL,
        values={"site": "{email}", "email": "leaked@example.test"},
    )

    assert "leaked@example.test" not in recorded.read_text()


async def test_a_login_missing_a_field_it_needs_is_not_run_and_says_which(
    tmp_path: Path,
) -> None:
    """Running it would fail in the binary's words about a flag nobody set."""
    recorded = tmp_path / "argv.txt"
    requirement = _argv_recording_requirement(recorded, "--site={site} --email={email}")

    result = await run_token_login(
        requirement,
        token=_SENTINEL,
        caller_did=_CALLER,
        audit_sink=_RecordingSink(),
        tier=Tier.PERSONAL,
        values={"site": "ctgfederal.atlassian.net"},
    )

    assert not result.completed
    assert "email" in result.detail
    assert not recorded.exists()


async def test_a_login_needing_no_field_still_runs_with_nothing_supplied() -> None:
    """`gh auth login --with-token` must not acquire a requirement it never had."""
    result = await _login(_requirement(_ACCEPTS), _SENTINEL, _RecordingSink())

    assert result.completed


# --- the token arrives as the operator meant it -------------------------------
#
# The reported failure, in its new home. An operator pasted a real Atlassian token
# beside a real address and got `401 Unauthorized`: a token copied out of a browser
# dialog carries a trailing newline or a non-breaking space the operator cannot
# see, and a value stored byte for byte took the invisible character all the way to
# the service.
#
# `field_formats` closed that for a token Arc STORES. A token that crosses on stdin
# is stored nowhere and declares nothing, so the same paste reached the binary
# untouched — and `echo <token> |`, which is the form Atlassian's own help gives,
# appends a newline every time. So the one place a token crosses to stdin applies
# the same shape `api_token` already means, and refuses in the same words when the
# value cannot be one.


async def test_a_token_pasted_with_a_trailing_newline_arrives_without_it(
    tmp_path: Path,
) -> None:
    """The commonest paste, and Atlassian's own documented invocation."""
    recorded = tmp_path / "stdin.txt"
    script = f"import sys; open({str(recorded)!r}, 'w').write(sys.stdin.read())"

    result = await _login(_requirement(script), f"{_SENTINEL}\n", _RecordingSink())

    assert result.completed
    assert recorded.read_text() == _SENTINEL


async def test_a_token_wrapped_in_invisible_characters_arrives_bare(tmp_path: Path) -> None:
    """A copied line brings a non-breaking space and a zero-width mark with it."""
    recorded = tmp_path / "stdin.txt"
    script = f"import sys; open({str(recorded)!r}, 'w').write(sys.stdin.read())"

    # Escaped rather than literal: these characters are invisible in a source
    # file too, and a reader has to be able to see which ones are under test.
    wrapped = f"\ufeff\u00a0{_SENTINEL}\u00a0\n"

    await _login(_requirement(script), wrapped, _RecordingSink())

    assert recorded.read_text() == _SENTINEL


async def test_a_token_with_an_invisible_character_inside_it_is_refused_not_repaired(
    tmp_path: Path,
) -> None:
    """The ends are where a paste picks characters up; the middle could be the value."""
    recorded = tmp_path / "stdin.txt"
    script = f"import sys; open({str(recorded)!r}, 'w').write(sys.stdin.read())"

    result = await _login(_requirement(script), "ATATT​sentinel", _RecordingSink())

    assert not result.completed
    assert not recorded.exists()
    assert "invisible" in result.detail


async def test_the_refusal_for_a_malformed_token_never_echoes_it() -> None:
    """It is rendered in a browser and written to a log."""
    result = await _login(_requirement(_ACCEPTS), f"{_SENTINEL}​{_SENTINEL}", _RecordingSink())

    assert not result.completed
    assert _SENTINEL not in result.detail


@pytest.mark.asyncio
async def test_a_child_that_exits_before_reading_stdin_is_a_verdict_not_a_crash(
    tmp_path: Path,
) -> None:
    """An immediate refusal is an ordinary answer, and must read as one.

    Writing to a pipe the child already closed raised out of the event loop and
    took the whole authorization check with it, so a connector that said no at
    once looked like a crash and denied the attach.
    """
    from arcagent.extension import host_login

    script = tmp_path / "refuses.sh"
    # Exits at once without reading stdin, which is what a CLI does when it has
    # nothing to ask about.
    script.write_text("#!/bin/sh\necho 'not logged in'\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)

    run = await host_login._capture(
        [str(script)],
        stdin_data="y\n" * 200_000,
        timeout=10.0,
        env={},
        timeout_hint="",
    )

    assert run.returncode is not None or run.text is not None
