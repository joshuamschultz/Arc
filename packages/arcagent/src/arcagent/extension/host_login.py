"""SPEC-064 — the two things Arc runs on a connector's own host binary.

**Signing in.** Most CLI logins cannot be completed by a button. ``gog auth add``
opens a browser, ``dbxcli login`` waits at a prompt, ``ms-365-mcp-server
--login`` prints a device code. A surface offering those a button would hang on
the prompt and then report a sign-in that never happened — worse than no button,
because the operator stops looking. So the manifest declares which kind a binary
has (:attr:`~arcagent.extension.manifest.HostRequirement.token_command`) and only
that kind is ever run here. Everything else is directed, never attempted.

**Asking whether it is signed in.** A connector's probe answers "does this
program run", and a bundle probing with a ``version`` subcommand answers it
happily on a binary holding no credential at all. Rendering that as *Signed in*
told an operator their account was connected when it was not, and sent them away
from the one action that would have connected it. So a manifest declares
:attr:`~arcagent.extension.manifest.HostRequirement.verify_command` separately —
``dbxcli account``, ``gh auth status``, ``gog auth list`` — and a bundle that
declares none reports :attr:`AuthorizationCheck.known` false rather than a guess.

Both verbs run a manifest-declared string, so both take the same three bounds,
which is why they live in one module: a second copy of these would be a second
set to keep in step, and the one that drifts is the one an attacker uses. They
close the hole :mod:`arcagent.extension.host` keeps shut by never running
``instruction`` at all:

* the command is split with :func:`shlex.split` and spawned with
  ``create_subprocess_exec``, so no shell parses it and ``;`` is an argument;
* ``argv[0]`` must be the very binary the requirement declares, so the field
  authorises one program and not a program of the manifest's choosing;
* the command is killed if it does not end, because one mis-declared as
  non-interactive would otherwise wait forever.

**The token crosses on stdin and nowhere else.** Never on argv — that is the
process table, readable by every other user on the box — and it appears in no
value this module returns, no log record, and no audit event. The one surface
Arc does not control is what the binary itself prints, so that is redacted.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass

from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.core.tier import Tier
from arcagent.extension.environment import scrubbed_environment
from arcagent.extension.manifest import HostRequirement
from arcagent.extension.secrets import Secret, redact

_logger = logging.getLogger("arcagent.extension.host_login")

#: How long a non-interactive login may take. Generous enough for a token
#: exchange over a slow link, short enough that a mis-declared interactive
#: command does not hold a request open.
_LOGIN_TIMEOUT_SECONDS = 60.0

#: What a binary's own output is truncated to before an operator reads it.
_DETAIL_LIMIT = 400

#: How long an authorisation check may take. Shorter than a login: it is taken on
#: every panel view, and a binary that has not answered in this long is one whose
#: state is unknown rather than one worth waiting on.
_CHECK_TIMEOUT_SECONDS = 20.0

_ACTION = "extension.host.authorize"

_CHECK_ACTION = "extension.host.verify"


@dataclass(frozen=True)
class LoginResult:
    """Whether the binary signed in, and the line to show the operator.

    ``detail`` is the binary's own output, truncated and with the token redacted.
    No field here can hold a credential.
    """

    completed: bool
    detail: str


@dataclass(frozen=True)
class AuthorizationCheck:
    """Whether this binary is signed in — and whether that could be established.

    ``known`` false is a real answer, not a failure: a bundle declaring no
    ``verify_command``, or one whose command names a different binary, leaves Arc
    with no evidence either way. Collapsing that into ``authorized`` would be the
    defect this type exists to close, and collapsing it into "signed out" would
    send an operator to redo a login that is already done.
    """

    authorized: bool
    known: bool
    detail: str


async def run_token_login(
    requirement: HostRequirement,
    *,
    token: str,
    caller_did: str,
    audit_sink: AuditSink,
    tier: Tier,
    timeout: float = _LOGIN_TIMEOUT_SECONDS,
) -> LoginResult:
    """Complete one host binary's sign-in with a token, or say plainly why it did not.

    Args:
        requirement: The declared prerequisite. Its ``token_command`` is the only
            command that runs, and only when ``argv[0]`` is ``name``.
        token: The operator's credential. Written to the child's stdin and to
            nothing else. It is not returned, logged, or audited.
        caller_did: The operator recorded as the actor on the verdict (Pillar 1).
        audit_sink: Where the verdict is recorded, either way.
        tier: Deployment stringency, stamped on the record.
        timeout: Seconds before the login is killed and reported as unfinished.

    Returns:
        The verdict. Never raises for a failed sign-in: an operator needs the
        reason, and a login that did not work is an answer, not an exception.
    """
    argv = _argv(requirement.token_command, requirement.name)
    if argv is None:
        return _record(
            requirement,
            caller_did=caller_did,
            sink=audit_sink,
            tier=tier,
            result=LoginResult(
                completed=False,
                detail=(
                    f"{requirement.name} has no sign-in Arc can complete on its own — "
                    f"run its authorising command on this host"
                ),
            ),
        )

    result = await _spawn(argv, token=token, timeout=timeout)
    return _record(requirement, caller_did=caller_did, sink=audit_sink, tier=tier, result=result)


def _argv(command: str, binary: str) -> list[str] | None:
    """The token list to exec, or ``None`` when this manifest string may not run.

    Three refusals, one place: nothing declared, a string no shell grammar parses
    (an unbalanced quote raises rather than yielding a guess), and a command whose
    ``argv[0]`` is not the very binary the requirement declares — the field
    authorises one program, not a program of the manifest's choosing.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    return argv if argv and argv[0] == binary else None


def authorization_verdict(requirement: HostRequirement, returncode: int, output: str) -> bool:
    """Read one finished check the way its manifest says to read it.

    Exit code first, because most binaries say it there: ``gh auth status`` and
    ``dbxcli account`` both exit non-zero when logged out. ``verify_pattern``
    covers the ones that do not — ``gog auth list`` exits zero with an empty
    listing, ``ms-365-mcp-server --verify-login`` exits zero and prints
    ``{"success":false}`` — and a check read on its exit code alone would call
    both of those signed in.

    Separate from running it so a bundle's recorded real output can be replayed
    through the very predicate the deployment uses.
    """
    if returncode != 0:
        return False
    if not requirement.verify_pattern:
        return True
    return re.search(requirement.verify_pattern, output) is not None


async def run_authorization_check(
    requirement: HostRequirement,
    *,
    caller_did: str,
    audit_sink: AuditSink,
    tier: Tier,
    env: Mapping[str, Secret] | None = None,
    timeout: float = _CHECK_TIMEOUT_SECONDS,
) -> AuthorizationCheck:
    """Ask one host binary whether it is signed in, or say plainly that Arc cannot.

    Args:
        requirement: The declared prerequisite. Its ``verify_command`` is the only
            command that runs, and only when ``argv[0]`` is ``name``.
        caller_did: The operator recorded as the actor on the verdict (Pillar 1).
        audit_sink: Where the verdict is recorded, either way.
        tier: Deployment stringency, stamped on the record.
        env: The connection's ``[secrets.placement]`` entries. A binary that reads
            its credential from its environment — ``dbxcli`` does — answers "signed
            out" without them, so a check taken outside the placed environment
            reports a just-connected account as not connected. The values are
            handed to the child and appear in no verdict, log line, or audit event.
        timeout: Seconds before the check is killed and reported as unknown.

    Returns:
        The verdict, with ``known`` false whenever nothing was established. Never
        raises: a connector that cannot be checked is an answer an operator needs,
        not an exception a panel turns into a 500.
    """
    argv = _argv(requirement.verify_command, requirement.name)
    if argv is None:
        return _record_check(
            requirement,
            caller_did=caller_did,
            sink=audit_sink,
            tier=tier,
            result=AuthorizationCheck(
                authorized=False,
                known=False,
                detail=f"{requirement.name} declares no way to check whether it is signed in",
            ),
        )

    run = await _capture(argv, stdin_data="", timeout=timeout, timeout_hint="", env=env)
    if run.returncode is None:
        result = AuthorizationCheck(authorized=False, known=False, detail=run.text)
    else:
        result = AuthorizationCheck(
            authorized=authorization_verdict(requirement, run.returncode, run.text),
            known=True,
            # No fallback to the binary's name: a caller names the command it ran,
            # and a check that printed nothing must not have that gap filled in
            # with something that reads like output.
            detail=_readable(run.text, ""),
        )
    return _record_check(
        requirement, caller_did=caller_did, sink=audit_sink, tier=tier, result=result
    )


def _record_check(
    requirement: HostRequirement,
    *,
    caller_did: str,
    sink: AuditSink,
    tier: Tier,
    result: AuthorizationCheck,
) -> AuthorizationCheck:
    """Hand one sign-in verdict to the single emission point. Coordinates only."""
    emit(
        AuditEvent(
            actor_did=caller_did,
            action=_CHECK_ACTION,
            target=f"host:{requirement.name}",
            outcome="allow" if result.authorized else "deny",
            tier=tier.value,
            extra={"binary": requirement.name, "known": result.known},
        ),
        sink,
    )
    return result


@dataclass(frozen=True)
class _Run:
    """One finished (or unfinished) subprocess.

    ``returncode`` is ``None`` when the binary never produced one — it could not
    be started, or it outran its deadline and was killed — and ``text`` is then
    the reason rather than the binary's output.
    """

    returncode: int | None
    text: str


async def _capture(
    argv: list[str],
    *,
    stdin_data: str,
    timeout: float,
    timeout_hint: str,
    env: Mapping[str, Secret] | None = None,
) -> _Run:
    """Run ``argv`` to completion, killing it if it does not end.

    ``stdin_data`` is written to the child and nowhere else. ``env`` carries the
    bundle's own ``[secrets.placement]`` entries, so a check reads the same
    credential the connection's verbs do; those stay wrapped until this call and
    are unwrapped straight into the child. The raw output comes back untruncated,
    because a caller matching a declared pattern against it must see all of it;
    truncation belongs to the line an operator reads.
    """
    placed = env or {}
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=scrubbed_environment({name: secret.reveal() for name, secret in placed.items()}),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:  # reason: a binary that is not on this host is a verdict, not a crash
        return _Run(returncode=None, text=f"{argv[0]} could not be run — {exc}")

    try:
        output, _ = await asyncio.wait_for(process.communicate(stdin_data.encode()), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return _Run(
            returncode=None, text=f"{argv[0]} did not finish in {timeout:g}s{timeout_hint}"
        )
    # Redacted before the text leaves this function, not at the render: a binary that
    # echoes what it was handed would otherwise put it in a pattern match, a detail
    # line an operator reads in a browser, and a log record.
    return _Run(
        returncode=process.returncode or 0,
        text=redact(
            output.decode("utf-8", "replace"), (secret.reveal() for secret in placed.values())
        ),
    )


async def _spawn(argv: list[str], *, token: str, timeout: float) -> LoginResult:
    """Run the login with the token on stdin, killing it if it does not end."""
    run = await _capture(
        argv,
        stdin_data=token,
        timeout=timeout,
        timeout_hint=" — its sign-in needs a person",
    )
    if run.returncode is None:
        return LoginResult(completed=False, detail=run.text)
    return LoginResult(completed=run.returncode == 0, detail=_readable(run.text, token) or argv[0])


def _readable(output: str, token: str) -> str:
    """The binary's own words, one block, truncated, with the token taken back out.

    Redaction is not belt-and-braces: several CLIs echo what they were given, and
    that output is rendered in a browser and written to a log.
    """
    text = redact(" ".join(output.split()), (token,))
    return text if len(text) <= _DETAIL_LIMIT else f"{text[: _DETAIL_LIMIT - 1]}…"


def _record(
    requirement: HostRequirement,
    *,
    caller_did: str,
    sink: AuditSink,
    tier: Tier,
    result: LoginResult,
) -> LoginResult:
    """Hand one verdict to the single emission point. Coordinates only, never a value."""
    emit(
        AuditEvent(
            actor_did=caller_did,
            action=_ACTION,
            target=f"host:{requirement.name}",
            outcome="allow" if result.completed else "deny",
            tier=tier.value,
            extra={"binary": requirement.name},
        ),
        sink,
    )
    _logger.info(
        "host login for %s %s", requirement.name, "completed" if result.completed else "refused"
    )
    return result


__all__ = [
    "AuthorizationCheck",
    "LoginResult",
    "authorization_verdict",
    "run_authorization_check",
    "run_token_login",
]
