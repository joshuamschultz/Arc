"""RunnerHost — owns the ArcFlow WorkflowRunner's lifecycle (SPEC-061 COMP-009).

Constructed inside :func:`arcgateway.bootstrap.build_for_embedded` — the agent
side of the fleet service — never from arcui's lifespan. That placement is
the whole point (SDD.md §9, PRD REQ-230): execution must never require the
dashboard process to be running. This module has zero import of ``arcui``
(enforced by ``tests/unit/test_workflow_runner_host.py``), so a headless
gateway still progresses workflow runs.

Singleton enforcement (REQ-231)
--------------------------------
Two runner instances must never advance the same run frontier. For v1 this is
an EXPLICIT, IN-PROCESS guard (a class-level slot that refuses a second
``start()``) — not a distributed lease. Per SDD.md (Risks and Mitigations,
citing Kleppmann): a lease alone is not safe under pauses/partitions, so this
guard is a deliberately stated ceiling — one process, one runner — and
relaxing it to multi-process later requires fencing tokens, not just this
guard widened.

Merge-reconciliation note (SPEC-061 concurrent build)
------------------------------------------------------
``arcteam.workflows.runner`` (COMP-008) and its construction helper are being
implemented concurrently on a sibling branch. This module codes against the
SDD's documented contract (SDD.md COMP-008/COMP-011) via
:class:`WorkflowRunnerProtocol` and resolves the real implementation lazily
by name (``importlib``, never a static ``from arcteam.workflows... import
...``) so mypy --strict does not fail resolving a submodule that does not
exist yet in this checkout, and this module degrades to a clear, logged
no-op (fail-open — the rest of the gateway still boots) until arcteam's half
lands. At merge, reconcile ``_default_runner_factory`` against the real
``arcteam.workflows.runner.build_workflow_runner`` signature.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

_logger = logging.getLogger("arcgateway.workflow_runner_host")


@runtime_checkable
class WorkflowRunnerProtocol(Protocol):
    """Structural contract for ``arcteam.workflows.runner.WorkflowRunner`` (COMP-008).

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


class RunnerHost:
    """Owns exactly one running WorkflowRunner task for this process.

    The class-level ``_active`` slot is the singleton guard: two RunnerHost
    instances must never both drive the same frontier. This is explicitly a
    single-PROCESS guard, not a distributed lease — see the module docstring.
    """

    _active: RunnerHost | None = None

    def __init__(self, runner: WorkflowRunnerProtocol) -> None:
        self._runner = runner
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def active(cls) -> RunnerHost | None:
        """The process's currently-running RunnerHost, or ``None``."""
        return cls._active

    @classmethod
    async def start(cls, runner: WorkflowRunnerProtocol) -> RunnerHost:
        """Start ``runner.run_forever()`` as a background task; register the singleton.

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
        host = cls(runner)
        host._task = asyncio.create_task(host._run(), name="arcgateway:workflow-runner")
        cls._active = host
        return host

    async def _run(self) -> None:
        try:
            await self._runner.run_forever()
        except asyncio.CancelledError:
            raise
        except Exception:  # reason: a runner crash must not crash the gateway process
            _logger.exception("workflow runner task terminated unexpectedly")

    async def stop(self) -> None:
        """Cancel the runner task, close the runner, and clear the singleton slot."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # reason: shutdown must complete regardless of task-body errors
                _logger.exception("workflow runner task raised during shutdown")
        try:
            await self._runner.aclose()
        except Exception:  # reason: fail-open — shutdown must still clear the singleton slot
            _logger.exception("error closing workflow runner")
        if RunnerHost._active is self:
            RunnerHost._active = None


def _resolve_runner_key_path() -> Path:
    """The on-disk operator key path the runner's identity resolves from.

    Same file ``arcui/messaging.py``'s ``_operator_signer``/``_operator_messaging``
    resolve (packages/arcui/src/arcui/messaging.py:131-140) — one deployment
    operator key, never generated here (``generate_if_absent=False`` is
    arcteam's RunnerIdentity's job, COMP-011). This function only names the
    path.
    """
    from arcteam.config import default_config_dir

    return default_config_dir() / "operator" / "operator.key"


async def _default_runner_factory(*, tier: str, key_path: Path) -> WorkflowRunnerProtocol:
    """Build the real arcteam WorkflowRunner (COMP-008), resolved lazily.

    Only constructs an arcstore backend once ``arcteam.workflows.runner`` is
    confirmed present — a checkout without SPEC-061's arcteam half pays no
    extra cost. Raises :class:`RuntimeError` (never a bare ``ImportError``)
    so callers can fail open with a clear log line rather than crash the
    gateway boot.
    """
    try:
        module = importlib.import_module("arcteam.workflows.runner")
    except ImportError as exc:
        raise RuntimeError(
            "arcteam.workflows.runner.build_workflow_runner is not available — "
            "SPEC-061's arcteam engine (COMP-008/COMP-011) has not landed in "
            "this checkout. RunnerHost cannot start (see "
            "arcgateway.workflow_runner_host merge-reconciliation note)."
        ) from exc
    build_workflow_runner = getattr(module, "build_workflow_runner", None)
    if build_workflow_runner is None:
        raise RuntimeError(
            "arc gateway: arcteam.workflows.runner has no build_workflow_runner() "
            "— reconcile arcgateway.workflow_runner_host against the landed "
            "arcteam API."
        )

    from arcstore.backends.sqlite import SqliteBackend
    from arcstore.config import store_db_path

    backend = SqliteBackend(store_db_path(None))
    await backend.start()
    runner = build_workflow_runner(
        tier=tier,
        task_store_backend=backend,
        runner_key_path=key_path,
    )
    return cast(WorkflowRunnerProtocol, runner)


async def start_runner_host(
    *,
    tier: str,
    runner_factory: Any = None,
) -> RunnerHost | None:
    """Construct and start the singleton RunnerHost for this gateway process.

    Called from :func:`arcgateway.bootstrap.build_for_embedded` — the agent
    side of the fleet service (COMP-009). Never called from arcui.

    Fail-open (logged): when the arcteam workflow engine cannot be
    constructed (not yet landed in this checkout, or a genuine construction
    error), returns ``None`` so the rest of the gateway still boots — a
    deployment missing SPEC-061's arcteam half is a degraded-but-running
    gateway, not a crashed one. Returns the started :class:`RunnerHost`
    otherwise, or the already-active one if this process already has one
    (REQ-231 — never starts a second).
    """
    if RunnerHost.active() is not None:
        _logger.warning("start_runner_host: a runner is already active in this process")
        return RunnerHost.active()

    factory = runner_factory or _default_runner_factory
    try:
        runner = await factory(tier=tier, key_path=_resolve_runner_key_path())
    except RuntimeError:
        _logger.warning(
            "start_runner_host: arcteam workflow engine unavailable; "
            "workflows will not progress until it is installed",
            exc_info=True,
        )
        return None
    return await RunnerHost.start(runner)


__all__ = [
    "RunnerAlreadyActiveError",
    "RunnerHost",
    "WorkflowRunnerProtocol",
    "start_runner_host",
]
