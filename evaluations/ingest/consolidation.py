"""COMP-008 — drive and confirm one consolidation pass per session boundary.

``arcagent.modules.memory.capabilities._CONSOLIDATE_POLL_INTERVAL`` is a module
constant (``300.0``), so per-session consolidation cadence is unreachable by
config. The harness therefore fires each pass itself by awaiting the public
``consolidate_poll_once()`` — which is also why the background
``memory_consolidate_loop`` must stay unstarted for the whole run (REQ-184):
arcmemory holds no lock, so a background pass interleaved with a driven one
corrupts the shared manifest and the SQLite connection underneath it.

**The return value is not trusted.** ``consolidate_poll_once()`` returns ``True``
once it has *called* the brain, not once a pass has *landed*. Confirmation comes
from the two markers arcmemory writes to ``<workspace>/memory/`` (REQ-182):
``.consolidate-last-run`` must have advanced, and the write-ahead
``.consolidate-manifest.json`` must be gone — it exists only while a pass is
mid-flight, so a surviving manifest means the pass died partway or a second pass
is running concurrently.

**The warning handler is load-bearing, not diagnostics.** ``select_brain(...)``
is called without ``audit_sink=`` in ``arcagent.modules.memory._runtime``, so
every arcmemory-internal audit event is discarded in a live agent. The
``arcmemory.consolidate`` logger is then the *only* surviving channel carrying
``dedup_skipped`` and the degrade warnings that turn entity de-duplication into
a silent no-op — and a benchmark run whose de-dup silently died would report a
memory score for a subsystem that was not running. The waiter is a context
manager so that channel is captured for the whole run rather than per pass.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

from arcagent.core.agent_lifecycle import activate_runtime_bindings
from arcagent.modules.memory import _runtime
from arcagent.modules.memory.capabilities import consolidate_poll_once
from pydantic import BaseModel

if TYPE_CHECKING:
    from arcagent.core.agent import ArcAgent

CONSOLIDATE_LOGGER_NAME = "arcmemory.consolidate"
"""``arcmemory.consolidate`` uses ``getLogger(__name__)``; this is that name."""

MANIFEST_NAME = ".consolidate-manifest.json"
LAST_RUN_NAME = ".consolidate-last-run"


class ConsolidationStalledError(RuntimeError):
    """A driven consolidation pass did not land, so ingest must not continue.

    Continuing would attribute a missing memory to arcmemory's retrieval when the
    write side never completed — the benchmark would measure the wrong thing.
    """


class ConsolidationResult(BaseModel):
    """What one driven pass did, as observed from outside arcmemory."""

    fired: bool
    """What ``consolidate_poll_once()`` claimed. Recorded, never trusted."""

    last_run_before: datetime | None
    """``.consolidate-last-run`` before the pass; ``None`` on the first ever pass."""

    last_run_after: datetime
    """``.consolidate-last-run`` after the pass. Strictly greater than the before."""

    window_events: int
    """Captures pending when the pass fired — arcagent's own counter, since
    ``consolidate_poll_once()`` returns a bool and drops arcmemory's counts."""


class _WarningCollector(logging.Handler):
    """Appends WARNING+ records to a caller-owned list, formatted for the ledger."""

    def __init__(self, sink: list[str]) -> None:
        super().__init__(level=logging.WARNING)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink.append(f"{record.levelname} {record.name}: {record.getMessage()}")
        except Exception:  # reason: a logging handler must never break the run it observes
            self.handleError(record)


def _pending_capture_events() -> int:
    """Captures accumulated since the last consolidation, per arcagent's counter.

    Read before firing because ``consolidate_poll_once()`` zeroes it, and it is
    the only window size available to a caller outside arcmemory.
    """
    return _runtime.state().events_since_consolidate


class ConsolidationWaiter:
    """Fires one consolidation pass per session boundary and confirms it landed."""

    def __init__(self, *, agent: ArcAgent, workspace: Path) -> None:
        self._agent = agent
        self._memory_dir = Path(workspace) / "memory"
        self._collected: list[str] = []
        self._handler = _WarningCollector(self._collected)
        self._logger = logging.getLogger(CONSOLIDATE_LOGGER_NAME)
        self._restore_level: int | None = None
        self._attached = False

    @property
    def warnings(self) -> Sequence[str]:
        """Every WARNING+ record ``arcmemory.consolidate`` emitted during the run."""
        return tuple(self._collected)

    def __enter__(self) -> ConsolidationWaiter:
        self._logger.addHandler(self._handler)
        # An ancestor logger configured above WARNING would filter the degrade
        # records out before any handler sees them, so pin this logger down for
        # the run and put it back on exit.
        if self._logger.getEffectiveLevel() > logging.WARNING:
            self._restore_level = self._logger.level
            self._logger.setLevel(logging.WARNING)
        self._attached = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._logger.removeHandler(self._handler)
        if self._restore_level is not None:
            self._logger.setLevel(self._restore_level)
            self._restore_level = None
        self._attached = False

    async def wait(self) -> ConsolidationResult:
        """Fire one pass and confirm it landed, or raise ``ConsolidationStalledError``."""
        if not self._attached:
            raise RuntimeError(
                "ConsolidationWaiter.wait() outside its context manager would run the "
                "pass with the arcmemory.consolidate degrade channel unwatched"
            )
        if self._manifest_path.exists():
            raise ConsolidationStalledError(
                f"{MANIFEST_NAME} present before firing: a prior pass died mid-write or a "
                "second pass is in flight (REQ-184 — arcmemory has no lock)"
            )

        # The waiter runs in the harness's own task, not the sibling task a turn
        # was dispatched on, and memory's ``state()`` fails closed on an unbound DID.
        activate_runtime_bindings(self._agent)
        before = self._read_last_run()
        window_events = _pending_capture_events()
        fired = await consolidate_poll_once()
        after = self._read_last_run()

        if self._manifest_path.exists():
            raise ConsolidationStalledError(
                f"{MANIFEST_NAME} still present after the pass: it did not complete"
            )
        if after is None or (before is not None and after <= before):
            raise ConsolidationStalledError(
                f"{LAST_RUN_NAME} did not advance ({before} -> {after}) after a pass that "
                f"reported fired={fired} over {window_events} pending capture event(s)"
            )
        return ConsolidationResult(
            fired=fired,
            last_run_before=before,
            last_run_after=after,
            window_events=window_events,
        )

    @property
    def _manifest_path(self) -> Path:
        return self._memory_dir / MANIFEST_NAME

    def _read_last_run(self) -> datetime | None:
        """Parse the last-run stamp, or ``None`` when absent or unreadable.

        An unparseable stamp reads as "no pass landed" rather than as an advance,
        which keeps a corrupted marker on the stall path instead of the happy one.
        """
        path = self._memory_dir / LAST_RUN_NAME
        if not path.exists():
            return None
        try:
            return datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
        except ValueError:
            return None


__all__ = [
    "CONSOLIDATE_LOGGER_NAME",
    "LAST_RUN_NAME",
    "MANIFEST_NAME",
    "ConsolidationResult",
    "ConsolidationStalledError",
    "ConsolidationWaiter",
]
