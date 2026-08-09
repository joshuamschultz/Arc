"""SPEC-062 COMP-008 — one process path everywhere, with confinement supplied as a value.

REQ-292 is not "confine the process"; it is "there shall not be a second way to start
one". :class:`ProcessLauncher` therefore contains no deployment-stringency branch at
all — :func:`sandbox_policy_for` resolves a :class:`SandboxPolicy` object, and that
object is the only thing that differs between a laptop and a SCIF. A hardened
deployment swaps the policy; it does not take a different road. The alternative — a
"strict" launcher beside a "normal" one — means the hardened road is never driven
during development and is discovered to be broken in the place it matters most.

The default is genuine no-op confinement, so an unconfigured install spawns extension
processes with no host setup at all (Composability, audience 2). Stricter confinement
(a container, a microVM) is registered by the deployment through
:func:`register_sandbox_policy`; nothing here needs editing to add one.

Two controls ride the same single path:

* **Environment scrubbing (REQ-273).** Applied through
  :func:`~arcagent.extension.environment.scrubbed_environment`, which every path that
  starts a child on an extension's behalf shares — this launcher, the CLI attachment,
  and the sign-in check — so a variable refused on one is refused on all three.
* **Artifact pinning (REQ-290).** A definition naming a third-party artifact is
  verified against its pin before *every* start, not once at install. With no verifier
  configured, such a definition is refused rather than waved through.

Processes start lazily on first use and are reaped once idle past their threshold
(REQ-272) — a reap terminates the child, it does not merely drop the handle.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Protocol, runtime_checkable

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.environment import scrubbed_environment
from arcagent.extension.pin import ArtifactPinVerifier, PinnedArtifact

_logger = logging.getLogger("arcagent.extension.launcher")

#: How long an unused process is kept before :meth:`ProcessLauncher.reap_idle` stops it.
DEFAULT_IDLE_TIMEOUT_SECONDS = 300.0

#: How long a child gets to exit on its own after ``terminate`` before it is killed.
_STOP_GRACE_SECONDS = 5.0


@runtime_checkable
class SandboxPolicy(Protocol):
    """How an extension process is confined — a contract, never a concrete class.

    A deployment supplies confinement by providing an object with these two members.
    Adding a container or a microVM is therefore a new implementation, not an edit to
    :class:`ProcessLauncher`.
    """

    name: str
    """Short identifier for the confinement, recorded and shown to the operator."""

    def wrap(self, argv: list[str]) -> list[str]:
        """Return the argv that actually gets executed, confinement included."""
        ...


class NoConfinement:
    """The default policy: run the argv exactly as given.

    This is a real policy object, not a bypass. The launcher has no "unconfined" branch
    to fall into, so the code path exercised on a developer laptop is the code path a
    hardened deployment runs with a different object plugged in.
    """

    name = "none"

    def wrap(self, argv: list[str]) -> list[str]:
        """Hand the argv back untouched."""
        return list(argv)


#: Confinement per deployment stringency. Every entry starts at :class:`NoConfinement`
#: because Arc ships no container or microVM runtime of its own; a deployment that has
#: one registers it here (see :func:`register_sandbox_policy`).
_POLICIES: dict[Tier, SandboxPolicy] = {tier: NoConfinement() for tier in Tier}


def sandbox_policy_for(tier: Tier) -> SandboxPolicy:
    """Resolve the confinement policy for a deployment tier.

    Every tier resolves through this one lookup — that is what keeps stringency a dial
    rather than a fork. ``Tier.PERSONAL`` resolves to :class:`NoConfinement` by design
    (REQ-292); a stricter tier still holding the default is a deployment that has not
    registered its confinement yet, which is worth a warning but must not brick the
    install, so the process still starts on the same path.

    Args:
        tier: The deployment tier to resolve.

    Returns:
        The policy object the launcher will apply to every process it spawns.
    """
    policy = _POLICIES[tier]
    if tier is not Tier.PERSONAL and isinstance(policy, NoConfinement):
        _logger.warning(
            "no sandbox policy registered for %s tier; extension processes will run "
            "unconfined until one is registered",
            tier.value,
        )
    return policy


def register_sandbox_policy(tier: Tier, policy: SandboxPolicy) -> None:
    """Install the confinement a deployment provides for one tier.

    Args:
        tier: The deployment tier the policy applies to.
        policy: Any object satisfying :class:`SandboxPolicy`.
    """
    _POLICIES[tier] = policy


@dataclass(frozen=True)
class ProcessDefinition:
    """Everything needed to start one extension process, and nothing about how.

    Attributes:
        key: Identifies this process. Keys are per-connection, so one extension's
            process is never handed to another.
        argv: The command, before the policy wraps it.
        env: Variables the manifest asked for. Scrubbed like any other source — a
            declared ``LD_PRELOAD`` does not survive.
        cwd: Working directory for the child, if it needs a specific one.
        artifact: The third-party build this process runs, when there is one. Present
            means the pin is verified before every start; absent means the command is
            first-party and there is nothing third-party to pin.
        owner_did: The identity the process runs under, recorded on the pin verdict.
        idle_timeout_seconds: How long the process may sit unused before a reap.
    """

    key: str
    argv: list[str]
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    artifact: PinnedArtifact | None = None
    owner_did: str = ""
    idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS


@dataclass
class ProcessHandle:
    """A running extension process and the bookkeeping the reaper needs."""

    key: str
    process: asyncio.subprocess.Process
    idle_timeout_seconds: float
    last_used: float


class ProcessLauncher:
    """The one way an extension process is started.

    Args:
        policy: The confinement applied to every spawn. Supplied by the caller, never
            chosen here — see :func:`sandbox_policy_for`.
        verifier: Checks a pinned third-party artifact before each start. A definition
            naming an artifact is refused when this is absent, so an unconfigured
            launcher fails closed instead of running unverified bytes.
    """

    def __init__(
        self, *, policy: SandboxPolicy, verifier: ArtifactPinVerifier | None = None
    ) -> None:
        self._policy = policy
        self._verifier = verifier
        self._running: dict[str, ProcessHandle] = {}
        self._lock = asyncio.Lock()

    @property
    def running(self) -> tuple[str, ...]:
        """Keys of the processes currently alive under this launcher."""
        return tuple(self._running)

    async def acquire(self, definition: ProcessDefinition) -> ProcessHandle:
        """Return the process for ``definition``, starting it if it is not running.

        Nothing starts until the first call — holding a definition costs nothing — and
        a second call reuses the live process rather than restarting it, which is the
        point of a long-lived attachment.

        Args:
            definition: What to run and under what confinement bookkeeping.

        Returns:
            A handle whose ``process`` is alive.

        Raises:
            ExtensionError: The definition names a third-party artifact that failed
                verification, or that cannot be verified at all.
        """
        async with self._lock:
            handle = self._running.get(definition.key)
            if handle is not None and handle.process.returncode is None:
                handle.last_used = monotonic()
                return handle
            return await self._start(definition)

    async def reap_idle(self) -> tuple[str, ...]:
        """Stop every process that has sat unused past its threshold (REQ-272).

        Returns:
            The keys that were stopped. Each one is really terminated; forgetting the
            handle instead would leak a process per connection, per agent.
        """
        async with self._lock:
            now = monotonic()
            expired = [
                handle
                for handle in self._running.values()
                if now - handle.last_used >= handle.idle_timeout_seconds
            ]
            for handle in expired:
                await self._stop(handle)
            return tuple(handle.key for handle in expired)

    async def shutdown(self) -> None:
        """Stop every running process."""
        async with self._lock:
            for handle in list(self._running.values()):
                await self._stop(handle)

    async def _start(self, definition: ProcessDefinition) -> ProcessHandle:
        """Verify, scrub, wrap, spawn — in that order, on the only path there is."""
        if definition.artifact is not None:
            await asyncio.to_thread(self._verify, definition)
        process = await asyncio.create_subprocess_exec(
            *self._policy.wrap(list(definition.argv)),
            env=scrubbed_environment(definition.env),
            cwd=definition.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        handle = ProcessHandle(
            key=definition.key,
            process=process,
            idle_timeout_seconds=definition.idle_timeout_seconds,
            last_used=monotonic(),
        )
        self._running[definition.key] = handle
        return handle

    def _verify(self, definition: ProcessDefinition) -> None:
        """Refuse a third-party artifact that fails its pin, or that cannot be checked.

        Hashing is blocking work, so this runs off the event loop; it is a plain method
        rather than an inline branch so the fail-closed case reads as one statement.
        """
        artifact = definition.artifact
        if artifact is None:
            return
        if self._verifier is None:
            raise ExtensionError(
                code="ARTIFACT_UNVERIFIABLE",
                message=(
                    f"process '{definition.key}' runs third-party artifact "
                    f"'{artifact.pin.package}' but no pin verifier is configured"
                ),
                details={"key": definition.key, "package": artifact.pin.package},
            )
        self._verifier.verify(artifact, caller_did=definition.owner_did)

    async def _stop(self, handle: ProcessHandle) -> None:
        """Terminate one child, escalating to a kill if it ignores the request."""
        self._running.pop(handle.key, None)
        process = handle.process
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=_STOP_GRACE_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()


__all__ = [
    "DEFAULT_IDLE_TIMEOUT_SECONDS",
    "NoConfinement",
    "ProcessDefinition",
    "ProcessHandle",
    "ProcessLauncher",
    "SandboxPolicy",
    "register_sandbox_policy",
    "sandbox_policy_for",
]
