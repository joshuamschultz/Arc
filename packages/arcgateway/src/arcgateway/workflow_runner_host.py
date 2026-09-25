"""RunnerHost — owns the ArcFlow WorkflowRunner's lifecycle (SPEC-061 COMP-009).

Constructed inside :func:`arcgateway.bootstrap.build_for_embedded` — the agent
side of the fleet service — never from arcui's lifespan. That placement is
the whole point (SDD.md §9, PRD REQ-230): execution must never require the
dashboard process to be running. This module has zero import of ``arcui``
(enforced by ``tests/unit/test_workflow_runner_host.py``), so a headless
gateway still progresses workflow runs.

Distributed ownership
---------------------
The real runner renews a durable ArcStore lease and presents a monotonic fence
before every progress tick. ``_active`` below is only local lifecycle
bookkeeping; it is never the correctness boundary between gateway and CLI
processes.

Merge-reconciliation note (SPEC-061 concurrent build)
------------------------------------------------------
``arcteam.workflow.runner`` (COMP-008) and its construction helper are being
implemented concurrently on a sibling branch. This module codes against the
SDD's documented contract (SDD.md COMP-008/COMP-011) via
:class:`WorkflowRunnerProtocol` and resolves the real implementation lazily
by name (``importlib``, never a static ``from arcteam.workflow... import
...``) so mypy --strict does not fail resolving a submodule that does not
exist yet in this checkout, and this module degrades to a clear, logged
no-op (fail-open — the rest of the gateway still boots) until arcteam's half
lands. At merge, reconcile ``_default_runner_factory`` against the real
``arcteam.workflow.runner.build_workflow_runner`` signature.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

_logger = logging.getLogger("arcgateway.workflow_runner_host")
_RESTART_DELAY_SECONDS = 0.25
_RESTART_MAX_SECONDS = 30.0
_CLOSE_TIMEOUT_SECONDS = 5.0


@runtime_checkable
class WorkflowRunnerProtocol(Protocol):
    """Structural contract for ``arcteam.workflow.runner.WorkflowRunner`` (COMP-008).

    "Holds no model and makes no LLM call" (SDD.md COMP-008) — deterministic
    module code, a background loop analogous to the existing 5s reliability
    tick (``arcagent.modules.tasks`` reliability watcher).
    """

    async def run_forever(self) -> None:
        """Tick until cancelled: materialize the frontier, advance runs, narrate."""
        ...

    async def aclose(self) -> None:
        """Release resources (store handles, in-flight cancellation) on shutdown."""
        ...


class RunnerAlreadyActiveError(RuntimeError):
    """Raised when a second RunnerHost tries to start in this process (REQ-231)."""


class WorkflowEngineUnavailableError(RuntimeError):
    """The optional workflow engine is absent from this installation."""


class RunnerHost:
    """Owns exactly one running WorkflowRunner task for this process.

    The class-level ``_active`` slot tracks this process's task for callers;
    ArcStore's fenced lease remains the cross-process correctness boundary.
    """

    _active: RunnerHost | None = None

    def __init__(
        self,
        runner: WorkflowRunnerProtocol | None,
        factory: Callable[[], Awaitable[WorkflowRunnerProtocol]],
    ) -> None:
        self._runner = runner
        self._factory = factory
        self._task: asyncio.Task[None] | None = None
        self._ready = runner is not None
        self._poisoned = False
        self._stopping = False

    @classmethod
    def active(cls) -> RunnerHost | None:
        """The process's currently-running RunnerHost, or ``None``."""
        host = cls._active
        return host if host is not None and host._ready else None

    @property
    def available(self) -> bool:
        """Whether this host currently drives a live runner."""
        return self._ready and self._task is not None and not self._task.done()

    @property
    def runner(self) -> WorkflowRunnerProtocol:
        """The live WorkflowRunner this host drives.

        Exposed so another surface in this process — the slash-command
        workflow provider — composes its control plane onto the SAME store,
        run plane, and tier the engine already enforces, instead of building a
        second set that could disagree.
        """
        if self._runner is None:
            raise RuntimeError("workflow runner is unavailable")
        return self._runner

    @classmethod
    async def start(cls, factory: Callable[[], Awaitable[WorkflowRunnerProtocol]]) -> RunnerHost:
        """Build and supervise one runner with the process singleton.

        Raises:
            RunnerAlreadyActiveError: a RunnerHost is already active in this
                process — refuses to start rather than racing the frontier
                (REQ-231). Callers that legitimately need to restart (tests)
                must ``await host.stop()`` the prior instance first.
        """
        if cls._active is not None:
            raise RunnerAlreadyActiveError(
                "a WorkflowRunner is already active in this process; refusing "
                "to start a second instance — two runners must never advance "
                "the same frontier (REQ-231)"
            )
        host = cls(None, factory)
        cls._active = host
        try:
            try:
                runner = await factory()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _logger.warning("workflow runner startup failed (%s)", type(exc).__name__)
                runner = None
            host._runner = runner
            host._ready = runner is not None
            host._task = asyncio.create_task(host._run(), name="arcgateway:workflow-runner")
        except BaseException:
            if cls._active is host:
                cls._active = None
            raise
        return host

    async def _run(self) -> None:
        delay = _RESTART_DELAY_SECONDS
        try:
            while True:
                runner = self._runner
                if runner is None:
                    delay = await self._rebuild(delay)
                    continue
                started_at = asyncio.get_running_loop().time()
                try:
                    await runner.run_forever()
                    _logger.error("workflow runner returned unexpectedly")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _logger.error("workflow runner failed (%s)", type(exc).__name__)
                finally:
                    self._ready = False
                    if not await self._close_runner(runner):
                        self._poisoned = True
                if self._poisoned:
                    _logger.critical("workflow runner cleanup failed; refusing replacement")
                    return
                self._runner = None
                uptime = asyncio.get_running_loop().time() - started_at
                delay = (
                    _RESTART_DELAY_SECONDS
                    if uptime >= 60.0
                    else min(delay * 2, _RESTART_MAX_SECONDS)
                )
                delay = await self._rebuild(delay)
        finally:
            self._ready = False
            if RunnerHost._active is self and not self._poisoned:
                RunnerHost._active = None

    async def _rebuild(self, delay: float) -> float:
        while True:
            await asyncio.sleep(
                min(delay * (0.9 + secrets.randbelow(201) / 1000), _RESTART_MAX_SECONDS)
            )
            try:
                replacement = await self._factory()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _logger.error("workflow runner rebuild failed (%s)", type(exc).__name__)
                delay = min(delay * 2, _RESTART_MAX_SECONDS)
                continue
            if self._stopping:
                if not await self._close_runner(replacement):
                    self._poisoned = True
                raise asyncio.CancelledError
            self._runner = replacement
            self._ready = True
            _publish_to_agent_tools(replacement)
            return delay

    @staticmethod
    async def _close_runner(runner: WorkflowRunnerProtocol) -> bool:
        try:
            await asyncio.wait_for(runner.aclose(), timeout=_CLOSE_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            _logger.error("workflow runner close was cancelled")
            return False
        except Exception as exc:
            _logger.error("workflow runner close failed (%s)", type(exc).__name__)
            return False
        return True

    async def stop(self) -> None:
        """Cancel the runner task, close the runner, and clear the singleton slot."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=_CLOSE_TIMEOUT_SECONDS + 1.0)
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                self._poisoned = True
                _logger.critical("workflow runner shutdown timed out; singleton remains fenced")
            except Exception:  # reason: shutdown must complete regardless of task-body errors
                _logger.exception("workflow runner task raised during shutdown")
        if self._ready and self._runner is not None:
            self._ready = False
            if not await self._close_runner(self._runner):
                self._poisoned = True
        self._ready = False
        if RunnerHost._active is self and not self._poisoned:
            RunnerHost._active = None


def _resolve_runner_key_path() -> Path:
    """The on-disk operator key path the runner's identity resolves from.

    Same file ``arcui/messaging.py``'s ``_operator_signer``/``_operator_messaging``
    resolve (packages/arcui/src/arcui/messaging.py:131-140) — one deployment
    operator key, never generated here (``generate_if_absent=False`` is
    arcteam's RunnerIdentity's job, COMP-011). This function only names the
    path.
    """
    from arctrust.paths import default_operator_key_path

    return default_operator_key_path()


async def _default_runner_factory(*, tier: str, key_path: Path) -> WorkflowRunnerProtocol:
    """Build the real arcteam WorkflowRunner (COMP-008), resolved lazily.

    Only constructs an arcstore backend once ``arcteam.workflow.runner`` is
    confirmed present — a checkout without SPEC-061's arcteam half pays no
    extra cost. Raises :class:`RuntimeError` (never a bare ``ImportError``)
    so callers can fail open with a clear log line rather than crash the
    gateway boot.
    """
    try:
        module = importlib.import_module("arcteam.workflow.runner")
    except ImportError as exc:
        raise WorkflowEngineUnavailableError(
            "arcteam.workflow.runner.build_workflow_runner is not available — "
            "SPEC-061's arcteam engine (COMP-008/COMP-011) has not landed in "
            "this checkout. RunnerHost cannot start (see "
            "arcgateway.workflow_runner_host merge-reconciliation note)."
        ) from exc
    build_workflow_runner = getattr(module, "build_workflow_runner", None)
    if build_workflow_runner is None:
        raise WorkflowEngineUnavailableError(
            "arc gateway: arcteam.workflow.runner has no build_workflow_runner() "
            "— reconcile arcgateway.workflow_runner_host against the landed "
            "arcteam API."
        )

    from arcstore.backends import open_backend

    backend = open_backend()
    await backend.start()
    try:
        owners, narrator = await _team_bindings(key_path)
        runner = build_workflow_runner(
            tier=tier,
            task_store_backend=backend,
            runner_key_path=key_path,
            registry=owners,
            narrator=narrator,
        )
    except Exception:
        await backend.stop()
        raise
    return cast(WorkflowRunnerProtocol, runner)


async def _team_bindings(key_path: Path) -> tuple[Any, Any]:
    """Owner resolution and narration, on the SAME bus the agents use.

    Without these two a runner starts healthy and is useless: it resolves no
    node owner, so every run fails at its first node, and it narrates nowhere,
    so the channel half of the design is silently absent. Both are the same
    dead-wiring failure as never starting at all, one layer in.

    Raises if the configured authority or bus is unavailable. Starting an
    ownerless runner would make the workflow surface look healthy while every
    node remains unable to advance.
    """
    from arcteam.composition import make_backend
    from arcteam.workflow.identity import RunnerIdentity
    from arcteam.workflow.stores import build_team_bindings

    identity = RunnerIdentity.load(key_path)
    team_backend = await make_backend(_nats_url())
    try:
        owners, narrator = await build_team_bindings(
            backend=team_backend,
            operator_signer=_operator_signer(key_path),
            identity=identity,
        )
    except Exception:
        close = getattr(team_backend, "close", None)
        if close is not None:
            await close()
        raise
    return owners, narrator


#: One resolver, ``arcteam.config.default_nats_url``, shared with the agents,
#: the ``arc team`` CLI, the dashboard, and the broker the gateway starts. This
#: MUST match: the runner resolves node owners from the team registry and
#: narrates to team channels, so a runner on a different bus than the agents is
#: a separate island — it would resolve no owner, fail every run at its first
#: node, and post narration nobody receives. That failure is silent and looks
#: healthy, which is the shape this feature has produced five times.
def _nats_url() -> str:
    """The messaging substrate url the AGENTS are on — never a private default."""
    from arcteam.config import default_nats_url

    return default_nats_url()


def _operator_signer(key_path: Path) -> Any:
    """The deployment authority that signs the messaging audit chain (AU-9/10)."""
    from arctrust import OperatorKey

    return OperatorKey.load(key_path, generate_if_absent=False).into_signer()


async def start_runner_host(
    *,
    tier: str,
    runner_factory: Any = None,
) -> RunnerHost | None:
    """Construct and start the singleton RunnerHost for this gateway process.

    Called from :func:`arcgateway.bootstrap.build_for_embedded` — the agent
    side of the fleet service (COMP-009). Never called from arcui.

    Construction failures leave one degraded host that retries with bounded
    backoff. The rest of the gateway can boot, while the runner remains
    unavailable until its required bindings are restored.
    """
    if RunnerHost._active is not None:
        _logger.warning("start_runner_host: a runner is already active in this process")
        return RunnerHost._active

    factory = runner_factory or _default_runner_factory

    async def _build() -> WorkflowRunnerProtocol:
        return cast(
            WorkflowRunnerProtocol,
            await factory(tier=tier, key_path=_resolve_runner_key_path()),
        )

    host = await RunnerHost.start(_build)
    if host.available:
        _publish_to_agent_tools(host.runner)
    return host


def _publish_to_agent_tools(runner: Any) -> None:
    """Hand the started runner to the agent-side ``workflow_run`` tool.

    Without this the two halves both look healthy and the feature still does
    nothing: the gateway hosts a live runner, the agent's tool reports that no
    runner is hosted here, and nothing errors. That is the same silent shape as
    the plural-module-name bug — a seam where each side is individually correct
    and the connection between them was never made.

    Best-effort by design: arcagent's workflows module is optional and may be
    disabled, so an absent module is a debug line, not a boot failure. A runner
    that started but could not be published is a WARNING, because that
    combination means runs will be refused by a deployment that looks fine.
    """
    try:
        import arcagent
    except ImportError:
        _logger.debug(
            "workflow runner started but arcagent's workflows module is absent; "
            "nothing to publish to"
        )
        return
    try:
        arcagent.set_workflow_runner(runner)
    except Exception:  # reason: publishing must not take down a healthy gateway
        _logger.warning(
            "workflow runner started but could not be published to the agent tool "
            "surface; workflow_run will report no runner despite one running",
            exc_info=True,
        )


__all__ = [
    "RunnerAlreadyActiveError",
    "RunnerHost",
    "WorkflowRunnerProtocol",
    "start_runner_host",
]
