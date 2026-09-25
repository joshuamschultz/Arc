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

**Signing in from a browser.** A third kind is neither a token nor a person at
the host: a manifest-declared ``remote_login`` whose first step prints a consent
link and whose second exchanges the address the operator's browser lands on. Both
steps run here, under the same bounds, and every check on what they are given is
in :mod:`arcagent.extension.remote_login` — including why the pasted address,
which carries a single-use PKCE-bound code, is acceptable as an argv value when a
token never is.

**The token crosses on stdin and nowhere else.** Never on argv — that is the
process table, readable by every other user on the box — and it appears in no
value this module returns, no log record, and no audit event. The one surface
Arc does not control is what the binary itself prints, so that is redacted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.environment import scrubbed_environment
from arcagent.extension.field_formats import normalize
from arcagent.extension.manifest import (
    REDIRECT_URL_SLOT,
    HostRequirement,
    fill_placeholders,
    placeholders,
)
from arcagent.extension.remote_login import (
    ConsentLink,
    PendingLogin,
    checked_argv_value,
    checked_redirect_url,
    consent_link,
)
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

_BEGIN_ACTION = "extension.host.remote_login.begin"

_COMPLETE_ACTION = "extension.host.remote_login.complete"


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
    #: True when the check failed in the way the manifest's
    #: ``verify_expired_pattern`` says a stored-but-dead credential fails: an
    #: account that needs reconnecting, not one that was never connected.
    expired: bool = False


@dataclass(frozen=True)
class RemoteLoginStep:
    """What one step of a remote sign-in did, and the line to show the operator.

    ``link`` is set only by a begin that produced a consent link on the declared
    host. ``reason`` is a coordinate for the audit record — never a value.
    """

    completed: bool
    detail: str
    link: ConsentLink | None = None
    reason: str = ""


async def run_token_login(
    requirement: HostRequirement,
    *,
    token: str,
    caller_did: str,
    audit_sink: AuditSink,
    tier: Tier,
    values: Mapping[str, str] | None = None,
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
        values: The bundle's own non-sensitive fields, for a login that needs more
            than a token — several take the site and the account address on argv
            beside it. Only the fields the manifest names are read, and the
            manifest may only name fields declared ``sensitive = false``, so this
            cannot become a second route for a credential onto argv.
        timeout: Seconds before the login is killed and reported as unfinished.

    Returns:
        The verdict. Never raises for a failed sign-in: an operator needs the
        reason, and a login that did not work is an answer, not an exception.
    """
    # The shape is applied HERE because there is nowhere else it could be. A token
    # that crosses on stdin is stored under no declared field, so it declares no
    # format — and a paste out of a browser dialog carries a trailing newline or a
    # non-breaking space nobody can see. That is the 401 an operator could not
    # explain, and `echo <token> |` — the vendor's own documented form — produces it
    # every time.
    try:
        token = normalize("api_token", "token", token) if token else token
    except ExtensionError as refusal:
        return _record(
            requirement,
            caller_did=caller_did,
            sink=audit_sink,
            tier=tier,
            result=LoginResult(completed=False, detail=refusal.message),
        )

    supplied = dict(values or {})
    missing = [name for name in placeholders(requirement.token_command) if not supplied.get(name)]
    if missing:
        return _record(
            requirement,
            caller_did=caller_did,
            sink=audit_sink,
            tier=tier,
            result=LoginResult(
                completed=False,
                detail=(
                    f"{requirement.name} needs {', '.join(missing)} as well as the token — "
                    f"supply {'them' if len(missing) > 1 else 'it'} and try again"
                ),
            ),
        )

    argv = _argv(requirement.token_command, requirement.name, supplied)
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


def _argv(command: str, binary: str, values: Mapping[str, str]) -> list[str] | None:
    """The token list to exec, or ``None`` when this manifest string may not run.

    Three refusals, one place: nothing declared, a string no shell grammar parses
    (an unbalanced quote raises rather than yielding a guess), and a command whose
    ``argv[0]`` is not the very binary the requirement declares — the field
    authorises one program, not a program of the manifest's choosing.

    Fields are filled in AFTER the split and within a single token, so a value
    carrying spaces, quotes or a leading ``--`` becomes part of the argument it was
    substituted into and can never become an argument of its own. One pass, so a
    value that happens to look like a placeholder is left as it was typed rather
    than reaching a field it was not given.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    filled = [fill_placeholders(token, values) for token in argv]
    return filled if filled and filled[0] == binary else None


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


def expired_verdict(requirement: HostRequirement, returncode: int, output: str) -> bool:
    """Whether a FAILED check failed the way a stored-but-dead credential does.

    Only ever true for a check that did not pass: a success that happens to print
    the pattern is still a success. Separate from :func:`authorization_verdict`
    for the same reason that is — recorded output replays through it.
    """
    if authorization_verdict(requirement, returncode, output):
        return False
    pattern = requirement.verify_expired_pattern
    return bool(pattern) and re.search(pattern, output) is not None


async def run_authorization_check(
    requirement: HostRequirement,
    *,
    caller_did: str,
    audit_sink: AuditSink,
    tier: Tier,
    env: Mapping[str, Secret] | None = None,
    visible: frozenset[str] = frozenset(),
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
        visible: Placed variables that carry no credential (an account address),
            left readable in the detail line.
        timeout: Seconds before the check is killed and reported as unknown.

    Returns:
        The verdict, with ``known`` false whenever nothing was established. Never
        raises: a connector that cannot be checked is an answer an operator needs,
        not an exception a panel turns into a 500.
    """
    argv = _argv(requirement.verify_command, requirement.name, {})
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

    run = await _capture(
        argv, stdin_data="", timeout=timeout, timeout_hint="", env=env, visible=visible
    )
    if run.returncode is None:
        result = AuthorizationCheck(authorized=False, known=False, detail=run.text)
    else:
        result = AuthorizationCheck(
            authorized=authorization_verdict(requirement, run.returncode, run.text),
            expired=expired_verdict(requirement, run.returncode, run.text),
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
            extra={"binary": requirement.name, "known": result.known, "expired": result.expired},
        ),
        sink,
    )
    return result


async def run_remote_login_begin(
    requirement: HostRequirement,
    *,
    values: Mapping[str, str],
    caller_did: str,
    audit_sink: AuditSink,
    tier: Tier,
    instance: str,
    env: Mapping[str, Secret] | None = None,
    visible: frozenset[str] = frozenset(),
    timeout: float = _LOGIN_TIMEOUT_SECONDS,
) -> RemoteLoginStep:
    """Run step one of a remote sign-in and hand back the consent link it printed.

    Args:
        requirement: The declared prerequisite. Its ``remote_login.begin`` is the
            only command that runs, and only when ``argv[0]`` is ``name``.
        values: The bundle's own non-sensitive fields; every one the step names
            must be present and pass :func:`~arcagent.extension.remote_login.
            checked_argv_value` before anything runs.
        caller_did: The operator recorded as the actor (Pillar 1).
        audit_sink: Where the verdict is recorded, either way.
        tier: Deployment stringency, stamped on the record.
        instance: The connection this sign-in is for — a coordinate on the record.
        env: The connection's ``[secrets.placement]`` entries, so the step acts as
            the same account and OAuth client the connection's verbs do.
        visible: Placed variables that carry no credential, left readable in
            what the binary prints.
        timeout: Seconds before the step is killed and reported as unfinished.

    Returns:
        The step. ``link`` is set only when the binary printed a link to the
        declared consent host. Never raises for a refused step.
    """
    login = requirement.remote_login
    argv, refusal = _step_argv(requirement, login.begin if login else "", values, "")
    if argv is None or login is None:
        return _record_step(
            requirement, _BEGIN_ACTION, instance, caller_did, audit_sink, tier, refusal
        )

    run = await _capture(
        argv, stdin_data="", timeout=timeout, timeout_hint="", env=env, visible=visible
    )
    if run.returncode != 0:
        step = RemoteLoginStep(
            completed=False,
            detail=_readable(run.text, "") or f"{requirement.name} could not start a sign-in",
            reason="step_failed",
        )
        return _record_step(
            requirement, _BEGIN_ACTION, instance, caller_did, audit_sink, tier, step
        )
    link = consent_link(run.text, login.consent_host)
    step = (
        RemoteLoginStep(
            completed=True,
            detail=f"Open the {login.consent_host} link and sign in as the connection's account.",
            link=link,
        )
        if link is not None
        else RemoteLoginStep(
            completed=False,
            detail=(
                f"{requirement.name} did not print a sign-in link to {login.consent_host}, "
                f"so Arc has nothing safe to open for you"
            ),
            reason="no_consent_link",
        )
    )
    return _record_step(requirement, _BEGIN_ACTION, instance, caller_did, audit_sink, tier, step)


async def run_remote_login_complete(
    requirement: HostRequirement,
    *,
    values: Mapping[str, str],
    redirect_url: str,
    expected: PendingLogin | None,
    caller_did: str,
    audit_sink: AuditSink,
    tier: Tier,
    instance: str,
    env: Mapping[str, Secret] | None = None,
    visible: frozenset[str] = frozenset(),
    timeout: float = _LOGIN_TIMEOUT_SECONDS,
) -> RemoteLoginStep:
    """Run step two with the address the operator pasted, or refuse it unrun.

    The address is checked first (:func:`~arcagent.extension.remote_login.
    checked_redirect_url`) against the sign-in ``expected`` began, and fills the
    reserved ``{redirect_url}`` slot as exactly one argument. It carries a
    single-use authorization code, so it — and the code inside it — is redacted
    from the binary's output and appears in no returned line, log record, or
    audit event.

    Returns:
        The step. ``completed`` is the binary's own exit verdict; whether the
        account now WORKS is a separate question the caller answers by running
        the declared check. Never raises for a refused step.
    """
    try:
        pasted = checked_redirect_url(redirect_url, expected=expected)
    except ExtensionError as refusal:
        step = RemoteLoginStep(completed=False, detail=refusal.message, reason="invalid_input")
        return _record_step(
            requirement, _COMPLETE_ACTION, instance, caller_did, audit_sink, tier, step
        )

    login = requirement.remote_login
    argv, refusal_step = _step_argv(requirement, login.complete if login else "", values, pasted)
    if argv is None:
        return _record_step(
            requirement, _COMPLETE_ACTION, instance, caller_did, audit_sink, tier, refusal_step
        )

    run = await _capture(
        argv, stdin_data="", timeout=timeout, timeout_hint="", env=env, visible=visible
    )
    spoken = redact(run.text, (pasted, *_query_values(pasted)))
    step = RemoteLoginStep(
        completed=run.returncode == 0,
        detail=_readable(spoken, "") or requirement.name,
        reason="" if run.returncode == 0 else "step_failed",
    )
    return _record_step(
        requirement, _COMPLETE_ACTION, instance, caller_did, audit_sink, tier, step
    )


def _query_values(url: str) -> tuple[str, ...]:
    """Every value in a pasted address's query, so a binary echoing one is redacted."""
    return tuple(value for _, value in parse_qsl(urlsplit(url).query) if len(value) >= 8)


def _step_argv(
    requirement: HostRequirement, command: str, values: Mapping[str, str], pasted: str
) -> tuple[list[str] | None, RemoteLoginStep]:
    """The argv for one step, or ``None`` and the refusal that says why not."""
    if not command:
        return None, RemoteLoginStep(
            completed=False,
            detail=f"{requirement.name} declares no sign-in Arc can run from a browser",
            reason="not_declared",
        )
    supplied: dict[str, str] = {}
    for field in placeholders(command):
        if field == REDIRECT_URL_SLOT:
            supplied[field] = pasted
            continue
        value = values.get(field, "")
        if not value:
            return None, RemoteLoginStep(
                completed=False,
                detail=f"set this connection's {field} first, then start the sign-in again",
                reason="missing_field",
            )
        try:
            supplied[field] = checked_argv_value(field, value)
        except ExtensionError as refusal:
            return None, RemoteLoginStep(
                completed=False, detail=refusal.message, reason="invalid_input"
            )
    argv = _argv(command, requirement.name, supplied)
    if argv is None:
        return None, RemoteLoginStep(
            completed=False,
            detail=f"{requirement.name}'s sign-in step does not invoke {requirement.name}",
            reason="not_declared",
        )
    return argv, RemoteLoginStep(completed=True, detail="")


def _record_step(
    requirement: HostRequirement,
    action: str,
    instance: str,
    caller_did: str,
    sink: AuditSink,
    tier: Tier,
    step: RemoteLoginStep,
) -> RemoteLoginStep:
    """Hand one sign-in step to the single emission point. Coordinates only.

    No link, no pasted address, no code: the record says who tried which step
    for which connection and how it ended — which is what an auditor needs and
    all that is safe to keep.
    """
    emit(
        AuditEvent(
            actor_did=caller_did,
            action=action,
            target=f"host:{requirement.name}",
            outcome="allow" if step.completed else "deny",
            tier=tier.value,
            extra={"binary": requirement.name, "connection": instance, "reason": step.reason},
        ),
        sink,
    )
    _logger.info(
        "remote sign-in %s for connection %s %s",
        action.rsplit(".", 1)[-1],
        instance,
        "completed" if step.completed else f"refused ({step.reason})",
    )
    return step


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
    visible: frozenset[str] = frozenset(),
) -> _Run:
    """Run ``argv`` to completion, killing it if it does not end.

    ``stdin_data`` is written to the child and nowhere else. ``env`` carries the
    bundle's own ``[secrets.placement]`` entries, so a check reads the same
    credential the connection's verbs do; those stay wrapped until this call and
    are unwrapped straight into the child. The raw output comes back untruncated,
    because a caller matching a declared pattern against it must see all of it;
    truncation belongs to the line an operator reads.

    ``visible`` names placed variables whose values are NOT credentials (an
    account address, a client name): those are left readable in the output, so
    "authorized as X, expected Y" still names Y. Every other placed value is
    redacted.
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
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return _Run(
            returncode=None, text=f"{argv[0]} did not finish in {timeout:g}s{timeout_hint}"
        )
    except (BrokenPipeError, ConnectionResetError, RuntimeError):
        # The child answered and exited before it read its stdin — an immediate
        # refusal, which is a perfectly ordinary verdict. Writing to the pipe it
        # already closed raised out of the event loop and took the whole
        # authorization check with it, so a connector that said no at once
        # looked like a crash.
        output = b""
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
    # Redacted before the text leaves this function, not at the render: a binary that
    # echoes what it was handed would otherwise put it in a pattern match, a detail
    # line an operator reads in a browser, and a log record.
    return _Run(
        returncode=process.returncode or 0,
        text=redact(
            output.decode("utf-8", "replace"),
            (secret.reveal() for name, secret in placed.items() if name not in visible),
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
    "RemoteLoginStep",
    "authorization_verdict",
    "expired_verdict",
    "run_authorization_check",
    "run_remote_login_begin",
    "run_remote_login_complete",
    "run_token_login",
]
