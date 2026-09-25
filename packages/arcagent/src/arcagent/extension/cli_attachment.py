"""CliAttachment — a vetted local binary as the default attachment (SPEC-062 COMP-005).

D-569 makes an operator-vetted CLI the default way an extension reaches an external
system, which moves one boundary out of a protocol library and into this file: with a
protocol server the transport framed the arguments; with a CLI *we* build argv, and
argv construction is where a connector turns into arbitrary command execution.

So the whole module is written around one rule: **an argument value is data, never
syntax.** Every token is placed into a list by this module, the list goes straight to
``exec``, and nothing here can hand a string to a command interpreter — there is no
such call site to audit, so a value carrying metacharacters, newlines, or a leading
``--`` is inert by construction rather than by escaping.

Flag arguments are encoded as one ``--flag=value`` token. The two-token form leaves a
value beginning with ``-`` ambiguous to the downstream parser, which is how a model
smuggles an extra flag into a command the operator declared; the single-token form has
no such reading, needs no conditionals, and refuses no legitimate value.

Failure splits two ways (REQ-270, REQ-271): a command that *ran and failed* — non-zero
exit, timeout, output that is not the declared JSON — becomes a readable
:class:`~arcagent.extension.attachment.ToolResult` the agent can reason about, while a
binary that could not be started at all raises, because there is no tool outcome to
report. The one shape never produced is a silent empty success.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arcagent.core.errors import ExtensionError
from arcagent.extension.attachment import (
    Classification,
    ProbeResult,
    Requirement,
    RequirementKind,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from arcagent.extension.environment import scrubbed_environment
from arcagent.extension.manifest import fill_placeholders
from arcagent.extension.secrets import Secret, redact

_logger = logging.getLogger(__name__)

#: The token that ends flag parsing. A manifest puts it last in a command's fixed
#: ``argv`` to make that command's positional arguments unreadable as flags.
_TERMINATOR = "--"

#: A whole positive integer, as a model would type it — no sign, no spaces, no
#: exponent, no leading zero.
_COUNT = re.compile(r"^[1-9][0-9]{0,8}$")

#: A plain file name a download may be saved under: no directory, no leading dot
#: or dash, nothing a filesystem or a parser reads as structure.
_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ ()+-]{0,127}$")


class _Declaration(BaseModel):
    """Base for what a manifest declares: frozen, and a typo is an error not a shrug."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class CliArgument(_Declaration):
    """One declared argument of a command, and the flag it is passed under.

    An empty ``flag`` is a POSITIONAL argument, which some verbs are only reachable
    through: a shipped connector wraps a binary whose read verb takes its record id
    positionally and answers the obvious flag with "unknown flag". It is legal only
    behind the ``--`` its command declares (:meth:`CliCommand.tokens`), because a
    bare token is otherwise read as syntax — ``--other-flag`` in a positional slot
    IS a flag to the parser.
    """

    name: str
    flag: str = ""
    description: str = ""
    #: A fixed shape the supplied value is placed into, as ``{value}``. Without
    #: it a whole argv token is whatever the caller passed, which for a verb
    #: like ``gh api <endpoint>`` is the entire read surface of an API. With it
    #: the manifest owns the shape and the caller fills one slot, so a crawl
    #: verb can be exposed without also granting everything beside it.
    template: str = ""
    #: A ceiling for a count argument (a page size). Set, the value must be a whole
    #: number from 1 to this, or the call is refused before anything runs.
    maximum: int | None = Field(default=None, ge=1)

    def refusal(self, value: object) -> str:
        """Why ``value`` may not be passed, or empty when it may."""
        if self.maximum is None:
            return ""
        text = str(value)
        if not _COUNT.fullmatch(text) or int(text) > self.maximum:
            return f"{self.name} must be a whole number from 1 to {self.maximum}"
        return ""

    @model_validator(mode="after")
    def _a_template_names_exactly_one_slot(self) -> CliArgument:
        """Refused at load: a template that drops the value silently sends a fixed
        token, and one that repeats it is almost never what was meant."""
        if not self.template:
            return self
        if self.template.count("{value}") != 1:
            raise ValueError(f"{self.name}: template must contain exactly one '{{value}}'")
        return self

    def token(self, value: object) -> str:
        """Render one argv token.

        ``--flag=value`` is a single token, so a value that looks like a flag can only
        ever be read as this flag's value — the downstream parser has no other reading.
        A positional carries no flag; what makes it unambiguous is the terminator its
        command is required to declare, not the token itself.
        """
        rendered = self.template.replace("{value}", str(value)) if self.template else f"{value}"
        return rendered if not self.flag else f"{self.flag}={rendered}"


class CliDownload(_Declaration):
    """A command that writes a file, and where Arc — never the model — puts it.

    ``argument`` is the declared argument carrying only a plain FILE NAME. Arc
    joins it to the attachment's download directory and passes the result under
    ``flag``; the name the model chose is never a path and never its own argv
    token. A file larger than ``max_bytes`` after the run is removed and the call
    fails.
    """

    argument: str
    flag: str = Field(pattern=r"^--[a-z][a-z0-9-]*$")
    max_bytes: int = Field(ge=1)


class CliCommand(_Declaration):
    """One command of the binary, exposed to the agent as its own named tool.

    ``argv`` is the fixed subcommand path the operator declared. It is manifest data and
    is never taken from the arguments a model supplies.
    """

    tool: str
    argv: list[str]
    arguments: list[CliArgument] = Field(default_factory=list)
    description: str = ""
    classification: Classification = "state_modifying"
    capability_tags: list[str] = Field(default_factory=list)
    #: What this command prints on success. ``json`` is the default and is checked;
    #: ``text`` is for the rare verb whose entire payload is one opaque value that
    #: no format flag can wrap — a secret resolve. Declared per command, so no other
    #: verb loses a check it depends on.
    output: Literal["json", "text"] = "json"
    #: The most a successful run may print. Above it the call fails with a
    #: readable reason instead of handing the model an unbounded payload (LLM10).
    max_output_bytes: int | None = Field(default=None, ge=1)
    download: CliDownload | None = None

    @model_validator(mode="after")
    def _a_download_names_a_plain_argument(self) -> CliCommand:
        """The download name is a declared, flagless, untemplated argument."""
        if self.download is None:
            return self
        match = [
            argument for argument in self.arguments if argument.name == self.download.argument
        ]
        if not match or match[0].flag or match[0].template:
            raise ValueError(
                f"{self.tool}: download.argument must name a declared argument with no flag "
                f"or template"
            )
        return self

    def _placed(self) -> list[CliArgument]:
        """The arguments that become argv tokens — every one but a download's name."""
        name = self.download.argument if self.download else None
        return [argument for argument in self.arguments if argument.name != name]

    @model_validator(mode="after")
    def _a_positional_is_only_legal_behind_a_terminator(self) -> CliCommand:
        """A bare value must be unreachable as syntax, and only ``--`` makes it so.

        Refused at load rather than at dispatch: a command that would place a model's
        value in a flag position is a defect in the manifest, and discovering it on
        the call that exploits it is discovering it too late.

        A command may take both, which real binaries commonly require — a search
        verb whose selector is a query and whose paging is flags. The order is
        what makes it safe, and :meth:`argv_for` owns it: every flag is emitted
        BEFORE the terminator and every positional after it, so a value can never
        land in a flag position and a flag can never be read as a value.
        """
        if all(argument.flag for argument in self._placed()):
            return self
        if _TERMINATOR not in self.argv:
            raise ValueError(f"{self.tool} takes a positional argument but declares no '--'")
        if self.argv[-1] != _TERMINATOR:
            raise ValueError(f"{self.tool} declares tokens after its '--', which are positionals")
        return self

    def spec(self) -> ToolSpec:
        """The registry-facing description, schema included.

        The schema is what the registry validates against, so it is built from the
        declared arguments and admits nothing else.
        """
        properties = {
            argument.name: {"type": "string", "description": argument.description}
            for argument in self.arguments
        }
        return ToolSpec(
            name=self.tool,
            description=self.description,
            input_schema={
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            },
            classification=self.classification,
            capability_tags=list(self.capability_tags),
        )

    def tokens(self, args: dict[str, Any]) -> list[str]:
        """Build this command's argument tokens, in declaration order.

        An undeclared key means validation upstream was bypassed, so it is refused
        rather than dropped. An omitted argument contributes no token at all: a dangling
        flag would swallow the following token and shift the whole parse.
        """
        declared = {argument.name: argument for argument in self.arguments}
        undeclared = sorted(set(args) - set(declared))
        if undeclared:
            raise ExtensionError(
                code="EXTENSION_REFUSED",
                message=f"{self.tool}: undeclared argument(s) {', '.join(undeclared)}",
                details={"tool": self.tool, "undeclared": undeclared},
            )
        return [argument.token(args[name]) for name, argument in declared.items() if name in args]

    def argv_for(self, args: dict[str, Any]) -> list[str]:
        """The full argument vector, with flags ahead of the terminator.

        The declared ``argv`` ends at the terminator when this command takes a
        positional, so flags cannot simply be appended: everything after ``--``
        is a value. They are spliced in before it instead, which is the only
        order in which a command taking both can be built safely.
        """
        declared = {argument.name: argument for argument in self._placed()}
        flags = [
            declared[name].token(args[name])
            for name in declared
            if name in args and declared[name].flag
        ]
        positionals = [
            declared[name].token(args[name])
            for name in declared
            if name in args and not declared[name].flag
        ]
        if not positionals:
            return [*self.argv, *flags]
        ends_with_terminator = bool(self.argv) and self.argv[-1] == _TERMINATOR
        head = list(self.argv[:-1]) if ends_with_terminator else list(self.argv)
        return [*head, *flags, _TERMINATOR, *positionals]


class CliResilience(_Declaration):
    """Bounds on how hard, and how often, Arc will try a binary that is misbehaving."""

    timeout_seconds: float = 60.0
    max_attempts: int = Field(default=3, ge=1)
    backoff_seconds: float = 0.5
    failure_threshold: int = Field(default=5, ge=1)
    reset_after_seconds: float = 30.0


class _CircuitBreaker:
    """Stops re-spawning a binary that keeps failing to start or finish.

    Counts consecutive *transport* failures only — a command that ran and exited
    non-zero is a working connection reporting a real answer, and never trips this.
    """

    def __init__(self, threshold: int, reset_after_seconds: float) -> None:
        self._threshold = threshold
        self._reset_after_seconds = reset_after_seconds
        self._failures = 0
        self._opened_at: float | None = None

    def retry_after(self) -> float | None:
        """Seconds until the circuit reopens, or ``None`` when a call may proceed.

        Once the window has elapsed the next call is let through on probation: a single
        further failure re-opens the circuit immediately.
        """
        if self._opened_at is None:
            return None
        remaining = self._reset_after_seconds - (time.monotonic() - self._opened_at)
        if remaining > 0:
            return remaining
        self._opened_at = None
        self._failures = self._threshold - 1
        return None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = time.monotonic()

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None


class CliAttachment:
    """Attaches an external system by running a locally installed, declared binary.

    Satisfies :class:`~arcagent.extension.attachment.ExtensionAttachment`, so a CLI
    connector reaches the agent through exactly the same four methods as every other
    kind of attachment.
    """

    def __init__(
        self,
        *,
        binary: str,
        commands: Sequence[CliCommand],
        probe_argv: Sequence[str] = ("--version",),
        install_instruction: str = "",
        resilience: CliResilience | None = None,
        env: Mapping[str, Secret] | None = None,
        values: Mapping[str, str] | None = None,
        owned_env: frozenset[str] = frozenset(),
        visible_env: frozenset[str] = frozenset(),
        download_dir: Path | None = None,
    ) -> None:
        self._binary = binary
        # Variables the bundle PLACES belong to this connection: set from its own
        # value or absent, never inherited from the service environment, where a
        # stray one would silently act as another account.
        self._owned_env = owned_env
        # Placed values that are settings, not credentials (an account address):
        # left readable in output, where redacting them hides which account answered.
        self._visible_env = visible_env
        # Where a download command may write. ``None`` refuses every download:
        # a surface that did not say where files go gets none.
        self._download_dir = download_dir
        # Configured fields are filled into the FIXED argv once, here, over manifest
        # data only. Doing it at construction rather than per call is what makes it
        # impossible for a model's value to be a substitution input or a target: by
        # the time a call arrives there is nothing left to expand.
        self._commands = {
            command.tool: command.model_copy(
                update={"argv": [fill_placeholders(token, values or {}) for token in command.argv]}
            )
            for command in commands
        }
        self._probe_argv = list(probe_argv)
        self._install_instruction = install_instruction
        self._resilience = resilience or CliResilience()
        # The credentials this bundle's ``[secrets.placement]`` entries asked for, kept
        # wrapped so a traceback or a ``vars()`` dump of this object renders
        # ``Secret(***)``. They are revealed at the spawn and nowhere else.
        self._env = dict(env or {})
        self._breaker = _CircuitBreaker(
            self._resilience.failure_threshold, self._resilience.reset_after_seconds
        )

    # --- the hook contract ---------------------------------------------------

    def requirements(self) -> list[Requirement]:
        """The binary is a host prerequisite: Arc directs, the operator installs."""
        return [
            Requirement(
                kind=RequirementKind.HOST,
                name=self._binary,
                instruction=self._install_instruction,
            )
        ]

    async def describe_tools(self) -> list[ToolSpec]:
        """One named tool per declared command — never one generic runner."""
        return [command.spec() for command in self._commands.values()]

    async def probe(self) -> ProbeResult:
        """Run the declared probe command; probing *is* the reachability test."""
        try:
            returncode, stdout, stderr = await self._spawn([self._binary, *self._probe_argv])
        except OSError as exc:
            return ProbeResult(
                reachable=False, detail=f"{self._binary} could not be started: {exc}"
            )
        if returncode != 0:
            return ProbeResult(
                reachable=False,
                detail=self._redacted(f"{self._binary} exited {returncode}: {stderr or stdout}"),
            )
        return ProbeResult(
            reachable=True,
            tools=await self.describe_tools(),
            detail=self._redacted((stdout or stderr).strip()),
        )

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Run one declared command and report what it did.

        argv is assembled here and nowhere else: the binary from the manifest, the fixed
        subcommand path from the manifest, then one token per supplied declared
        argument.
        """
        command = self._commands.get(tool)
        if command is None:
            raise ExtensionError(
                code="EXTENSION_REFUSED",
                message=f"{self._binary}: undeclared tool {tool!r}",
                details={"tool": tool, "declared": sorted(self._commands)},
            )
        command.tokens(args)  # refuses an undeclared argument before anything runs
        declared = {argument.name: argument for argument in command.arguments}
        for name, value in args.items():
            refusal = declared[name].refusal(value)
            if refusal:
                return self._error(tool, refusal)
        target: Path | None = None
        if command.download is not None:
            prepared = self._download_target(command.download, args)
            if isinstance(prepared, str):
                return self._error(tool, prepared)
            target = prepared
            args = {
                name: value for name, value in args.items() if name != command.download.argument
            }
        argv = [self._binary, *command.argv_for(args)]
        if target is not None and command.download is not None:
            argv = _with_flag(argv, f"{command.download.flag}={target}")

        retry_after = self._breaker.retry_after()
        if retry_after is not None:
            return self._error(
                tool,
                f"{self._binary} is unavailable after repeated transport failures; "
                f"retry in {retry_after:.0f}s",
            )
        try:
            returncode, stdout, stderr = await self._attempt(argv)
        except TimeoutError:
            return self._error(
                tool,
                f"{self._binary} {tool} did not finish within "
                f"{self._resilience.timeout_seconds:.0f}s",
            )
        result = self._result(command, returncode, stdout, stderr)
        if target is not None and command.download is not None:
            return _checked_download(result, target, command.download.max_bytes)
        return result

    def _download_target(self, download: CliDownload, args: dict[str, Any]) -> Path | str:
        """Where this download goes, or the reason it may not go anywhere."""
        if self._download_dir is None:
            return "this connection has no download directory, so it cannot save files"
        name = str(args.get(download.argument, ""))
        if not _FILE_NAME.fullmatch(name) or name in {".", ".."}:
            return (
                f"{download.argument} must be a plain file name (letters, digits, . _ - "
                f"space), with no folder in it"
            )
        directory = self._download_dir
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            return f"{name} already exists and is not a plain file; choose another name"
        return target

    # --- execution -----------------------------------------------------------

    async def _spawn(self, argv: list[str]) -> tuple[int, str, str]:
        """Hand the token list straight to ``exec`` and collect what it wrote.

        Raises ``OSError`` when the binary could not be started, and ``TimeoutError``
        when it outran its deadline — in which case the process is killed rather than
        left behind.
        """
        process = await asyncio.create_subprocess_exec(
            *argv,
            # The one place a placed credential is unwrapped: straight into the child's
            # environment, never onto argv, which every other user on the box can read.
            env=self._child_environment(),
            # No inherited stdin, ever. A vendor CLI that decides to prompt — a
            # confirmation, a missing required field, an editor — would otherwise
            # block on the service's stdin until the tool deadline and report a
            # timeout instead of its own error. Closed stdin turns that hang into
            # the CLI's immediate, readable refusal.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), self._resilience.timeout_seconds
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        return (
            process.returncode or 0,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )

    def _child_environment(self) -> dict[str, str]:
        """The placed values, the scrubbed inheritance, and no stray owned variable."""
        env = scrubbed_environment({name: secret.reveal() for name, secret in self._env.items()})
        for name in self._owned_env - set(self._env):
            env.pop(name, None)
        return env

    async def _attempt(self, argv: list[str]) -> tuple[int, str, str]:
        """Spawn with a bounded retry on timeouts, backing off between attempts.

        A binary that cannot be started is permanent, not transient, so it is reported
        at once instead of being tried again.
        """
        for attempt in range(self._resilience.max_attempts):
            if attempt:
                await asyncio.sleep(self._resilience.backoff_seconds * 2 ** (attempt - 1))
            try:
                returncode, stdout, stderr = await self._spawn(argv)
            except TimeoutError:
                self._breaker.record_failure()
            except OSError as exc:
                self._breaker.record_failure()
                raise ExtensionError(
                    code="EXTENSION_TRANSPORT_FAILED",
                    message=f"{self._binary} could not be started: {exc}",
                    details={"binary": self._binary},
                ) from exc
            else:
                self._breaker.record_success()
                return returncode, stdout, stderr
        raise TimeoutError(self._binary)

    # --- results -------------------------------------------------------------

    def _redacted(self, text: str) -> str:
        """The binary's own words with any placed credential taken back out.

        Applied to everything this class renders, because several CLIs echo the
        credential they were given into their error output — and that output is
        logged, shown to an operator, and put in front of the model as a tool
        result (LLM02).
        """
        return redact(
            text,
            (
                secret.reveal()
                for name, secret in self._env.items()
                if name not in self._visible_env
            ),
        )

    def _result(
        self, command: CliCommand, returncode: int, stdout: str, stderr: str
    ) -> ToolResult:
        """Turn one finished run into a result the agent can act on."""
        stdout, stderr = self._redacted(stdout), self._redacted(stderr)
        if stderr:
            # Countless CLIs write progress and warnings here on a perfectly good run,
            # so stderr is captured and surfaced to the operator but never itself
            # decides the outcome.
            _logger.warning("%s %s wrote to stderr: %s", self._binary, command.tool, stderr)

        if returncode != 0:
            return self._error(
                command.tool,
                f"{self._binary} {command.tool} exited {returncode}: "
                f"{stderr or stdout or '(no output)'}",
            )

        text = stdout.strip()
        size = len(text.encode("utf-8"))
        if command.max_output_bytes is not None and size > command.max_output_bytes:
            return self._error(
                command.tool,
                f"{self._binary} {command.tool} answered with {size} bytes, too large to "
                f"return (limit {command.max_output_bytes}); narrow the query or ask for "
                f"fewer results",
            )
        if not text:
            return self._error(
                command.tool,
                f"{self._binary} {command.tool} exited 0 and wrote nothing, so there is "
                f"no answer to report",
            )
        if command.output == "text":
            return ToolResult(tool=command.tool, outcome=ToolOutcome.OK, content=text)
        try:
            json.loads(text)  # parsed to validate the shape, not to transform it
        except ValueError:
            return self._error(
                command.tool,
                f"{self._binary} {command.tool} exited 0 but its output was not the "
                f"declared JSON: {text}",
            )
        return ToolResult(tool=command.tool, outcome=ToolOutcome.OK, content=text)

    @staticmethod
    def _error(tool: str, content: str) -> ToolResult:
        """A failure the agent reads — the tools stay registered either way."""
        return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content=content)


def _with_flag(argv: list[str], flag: str) -> list[str]:
    """``argv`` with ``flag`` placed ahead of any ``--`` terminator."""
    if _TERMINATOR in argv:
        at = argv.index(_TERMINATOR)
        return [*argv[:at], flag, *argv[at:]]
    return [*argv, flag]


def _checked_download(result: ToolResult, target: Path, max_bytes: int) -> ToolResult:
    """Keep the file only if it is a plain file within its bound."""
    if result.outcome is not ToolOutcome.OK:
        return result
    if target.is_symlink() or not target.is_file():
        target.unlink(missing_ok=True)
        return CliAttachment._error(result.tool, "the download did not produce a plain file")
    size = target.stat().st_size
    if size > max_bytes:
        target.unlink()
        return CliAttachment._error(
            result.tool, f"the file was larger than {max_bytes} bytes and was not kept"
        )
    return result


__all__ = ["CliArgument", "CliAttachment", "CliCommand", "CliDownload", "CliResilience"]
