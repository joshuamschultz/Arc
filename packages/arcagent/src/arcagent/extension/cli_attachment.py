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
import time
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
from arcagent.extension.secrets import Secret, redact

_logger = logging.getLogger(__name__)


class _Declaration(BaseModel):
    """Base for what a manifest declares: frozen, and a typo is an error not a shrug."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class CliArgument(_Declaration):
    """One declared argument of a command, and the flag it is passed under."""

    name: str
    flag: str
    description: str = ""

    def token(self, value: object) -> str:
        """Render one argv token.

        ``--flag=value`` is a single token, so a value that looks like a flag can only
        ever be read as this flag's value — the downstream parser has no other reading.
        """
        return f"{self.flag}={value}"


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
    ) -> None:
        self._binary = binary
        self._commands = {command.tool: command for command in commands}
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
        argv = [self._binary, *command.argv, *command.tokens(args)]

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
        return self._result(command, returncode, stdout, stderr)

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
            env=scrubbed_environment(
                {name: secret.reveal() for name, secret in self._env.items()}
            ),
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
        return redact(text, (secret.reveal() for secret in self._env.values()))

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
        if not text:
            return ToolResult(
                tool=command.tool,
                content=f"{self._binary} {command.tool} completed and wrote no output",
            )
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


__all__ = ["CliArgument", "CliAttachment", "CliCommand", "CliResilience"]
