"""SPEC-064 — the one connector sign-in Arc can finish without a human.

Most CLI logins cannot be completed by a button. ``gog auth add`` opens a
browser, ``dbxcli login`` waits at a prompt, ``ms-365-mcp-server --login`` prints
a device code. A surface offering those a button would hang on the prompt and
then report a sign-in that never happened — worse than no button, because the
operator stops looking. So the manifest declares which kind a binary has
(:attr:`~arcagent.extension.manifest.HostRequirement.token_command`) and only
that kind is ever run here. Everything else is directed, never attempted.

Three bounds keep a manifest string from becoming a way to run anything on the
host — the hole :mod:`arcagent.extension.host` keeps shut by never running
``instruction`` at all:

* the command is split with :func:`shlex.split` and spawned with
  ``create_subprocess_exec``, so no shell parses it and ``;`` is an argument;
* ``argv[0]`` must be the very binary the requirement declares, so the field
  authorises one program and not a program of the manifest's choosing;
* the login is killed if it does not end, because a command mis-declared as
  non-interactive would otherwise wait forever.

**The token crosses on stdin and nowhere else.** Never on argv — that is the
process table, readable by every other user on the box — and it appears in no
value this module returns, no log record, and no audit event. The one surface
Arc does not control is what the binary itself prints, so that is redacted.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass

from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.core.tier import Tier
from arcagent.extension.manifest import HostRequirement

_logger = logging.getLogger("arcagent.extension.host_login")

#: How long a non-interactive login may take. Generous enough for a token
#: exchange over a slow link, short enough that a mis-declared interactive
#: command does not hold a request open.
_LOGIN_TIMEOUT_SECONDS = 60.0

#: What a binary's own output is truncated to before an operator reads it.
_DETAIL_LIMIT = 400

#: What the token is replaced with wherever it would otherwise be shown.
_REDACTED = "***"

_ACTION = "extension.host.authorize"


@dataclass(frozen=True)
class LoginResult:
    """Whether the binary signed in, and the line to show the operator.

    ``detail`` is the binary's own output, truncated and with the token redacted.
    No field here can hold a credential.
    """

    completed: bool
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
    argv = shlex.split(requirement.token_command)
    if not argv or argv[0] != requirement.name:
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
    return _record(
        requirement, caller_did=caller_did, sink=audit_sink, tier=tier, result=result
    )


async def _spawn(argv: list[str], *, token: str, timeout: float) -> LoginResult:
    """Run the login with the token on stdin, killing it if it does not end."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:  # reason: a binary that is not on this host is a verdict, not a crash
        return LoginResult(completed=False, detail=f"{argv[0]} could not be run — {exc}")

    try:
        output, _ = await asyncio.wait_for(process.communicate(token.encode()), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return LoginResult(
            completed=False,
            detail=f"{argv[0]} did not finish in {timeout:g}s — its sign-in needs a person",
        )
    return LoginResult(
        completed=process.returncode == 0, detail=_readable(output, token) or argv[0]
    )


def _readable(output: bytes, token: str) -> str:
    """The binary's own words, one block, truncated, with the token taken back out.

    Redaction is not belt-and-braces: several CLIs echo what they were given, and
    that output is rendered in a browser and written to a log.
    """
    text = " ".join(output.decode("utf-8", errors="replace").split())
    if token:
        text = text.replace(token, _REDACTED)
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


__all__ = ["LoginResult", "run_token_login"]
