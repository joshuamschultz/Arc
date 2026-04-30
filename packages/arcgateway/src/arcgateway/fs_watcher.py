"""Per-agent filesystem watcher with ref-counted lazy lifecycle.

Lifecycle
---------
* :meth:`WatcherManager.subscribe` increments a per-agent refcount and starts
  the watch task on the 0→1 transition.
* :meth:`WatcherManager.unsubscribe` decrements; on N→0 the watcher task is
  cancelled and the entry is dropped. **No idle CPU when nobody's watching.**
* A configurable cap (``max_watchers``) prevents fork explosion across a large
  fleet (NF-7 / Pillar 4).

Event surface
-------------
The watcher inspects each detected change against :data:`_WATCH_MAP` to derive
the domain ``event_type`` (``policy:bullets_updated``, ``config:updated``,
etc.). Unrelated changes (anything not in the map) are dropped — the bus only
sees actionable events.

For ``policy:bullets_updated`` specifically, the watcher reparses
``policy.md`` once via :mod:`arcgateway.policy_parser` and ships the bullets in
the payload. Subscribers don't reparse.

Polling fallback
----------------
``watchfiles`` is the preferred backend (kqueue/inotify/IOCP, ~ms latency).
If it's not importable (or ``force_polling=True`` is passed for tests / for
deployments that disable C extensions), we fall back to a 2-second mtime poll
across the watched paths only. Same event surface, same payload format.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from arcgateway.audit import emit_event
from arcgateway.file_events import FileChangeEvent, FileEventBus, default_bus
from arcgateway.fs_reader import (
    FileTooLargeError,
    PathTraversalError,
    read_file,
)
from arcgateway.policy_parser import parse_bullets

logger = logging.getLogger(__name__)


# Watch-target → domain event_type. Order matters for prefix matching.
_WATCH_MAP: dict[str, str] = {
    "arcagent.toml": "config:updated",
    "workspace/identity.md": "memory:updated",
    "workspace/policy.md": "policy:bullets_updated",
    "workspace/context.md": "memory:updated",
    "workspace/pulse.md": "pulse:updated",
    "workspace/tasks.json": "tasks:updated",
    "workspace/schedules.json": "schedules:updated",
    # Directory prefixes — anything below these dirs maps to the event.
    "workspace/sessions": "session:changed",
    "workspace/memory": "memory:updated",
    "workspace/notes": "memory:updated",
    "workspace/skills": "skills:updated",
    # Live LLM activity. JSONLTraceStore appends to traces/traces-YYYY-MM-DD.jsonl
    # on every call. The arcui Overview Context Window card and Telemetry tab
    # re-render when this fires, so the % climbs as the agent runs.
    "traces": "traces:updated",
}


def match_event_type(rel: str) -> str | None:
    """Map a path (relative to agent root) to an event_type. ``None`` if untracked."""
    if rel in _WATCH_MAP:
        return _WATCH_MAP[rel]
    for prefix, evt in _WATCH_MAP.items():
        # Only treat keys that don't already point at a specific file as prefixes.
        if "." in Path(prefix).name:
            continue
        if rel == prefix or rel.startswith(prefix + "/"):
            return evt
    return None


@dataclass
class _WatcherEntry:
    agent_id: str
    agent_root: Path
    refcount: int = 0
    task: asyncio.Task[None] | None = None
    seen_mtimes: dict[Path, float] = field(default_factory=dict)


class WatcherManager:
    """Manages a pool of per-agent filesystem watchers.

    Args:
        bus: :class:`FileEventBus` to publish events on. Defaults to the
            module-level ``default_bus``.
        max_watchers: Hard cap on concurrent watchers across all agents.
        poll_interval: Seconds between polls when using the polling fallback.
        force_polling: When ``True``, skip ``watchfiles`` even if importable.
            Set in tests for determinism.
    """

    def __init__(
        self,
        *,
        bus: FileEventBus | None = None,
        max_watchers: int = 100,
        poll_interval: float = 2.0,
        force_polling: bool = False,
    ) -> None:
        self._bus = bus if bus is not None else default_bus
        self._max_watchers = max_watchers
        self._poll_interval = poll_interval
        self._force_polling = force_polling
        self._entries: dict[str, _WatcherEntry] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def has_watcher(self, agent_id: str) -> bool:
        return agent_id in self._entries

    def refcount(self, agent_id: str) -> int:
        entry = self._entries.get(agent_id)
        return entry.refcount if entry else 0

    async def subscribe(self, agent_id: str, agent_root: Path) -> None:
        """Increment the refcount and start a watcher if needed."""
        async with self._lock:
            entry = self._entries.get(agent_id)
            if entry is None:
                if len(self._entries) >= self._max_watchers:
                    raise RuntimeError(
                        f"max watchers reached: {self._max_watchers}"
                    )
                entry = _WatcherEntry(agent_id=agent_id, agent_root=agent_root.resolve())
                self._entries[agent_id] = entry
            entry.refcount += 1
            if entry.task is None:
                entry.task = asyncio.create_task(
                    self._run(entry), name=f"fs_watcher:{agent_id}"
                )

    async def unsubscribe(self, agent_id: str) -> None:
        """Decrement the refcount; tear down on 0."""
        async with self._lock:
            entry = self._entries.get(agent_id)
            if entry is None:
                return
            entry.refcount -= 1
            if entry.refcount <= 0:
                if entry.task is not None:
                    entry.task.cancel()
                self._entries.pop(agent_id, None)

    async def shutdown(self) -> None:
        """Cancel every running watcher. Safe to call multiple times."""
        async with self._lock:
            tasks: list[asyncio.Task[None]] = []
            for entry in self._entries.values():
                if entry.task is not None:
                    entry.task.cancel()
                    tasks.append(entry.task)
            self._entries.clear()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning(
                    "fs_watcher: exception during shutdown of task %r",
                    task,
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Watch loops
    # ------------------------------------------------------------------

    async def _run(self, entry: _WatcherEntry) -> None:
        if self._force_polling:
            await self._poll_loop(entry)
            return
        try:
            from watchfiles import awatch
        except ImportError:
            await self._poll_loop(entry)
            return

        try:
            async for changes in awatch(entry.agent_root, recursive=True):
                for _change_type, path_str in changes:
                    await self._dispatch(entry, Path(path_str))
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning(
                "fs_watcher[%s]: watchfiles loop error: %s", entry.agent_id, exc
            )

    async def _poll_loop(self, entry: _WatcherEntry) -> None:
        # Seed mtimes once so the first delta after subscribe is a real change.
        self._record_baseline(entry)
        try:
            while True:
                await asyncio.sleep(self._poll_interval)
                await self._poll_once(entry)
        except asyncio.CancelledError:
            return

    def _record_baseline(self, entry: _WatcherEntry) -> None:
        for target in self._iter_watched_files(entry):
            try:
                entry.seen_mtimes[target] = target.stat().st_mtime
            except OSError:
                continue

    async def _poll_once(self, entry: _WatcherEntry) -> None:
        for target in self._iter_watched_files(entry):
            try:
                m = target.stat().st_mtime
            except OSError:
                continue
            if entry.seen_mtimes.get(target) != m:
                entry.seen_mtimes[target] = m
                await self._dispatch(entry, target)

    def _iter_watched_files(self, entry: _WatcherEntry) -> list[Path]:
        """All files currently of interest under the agent root."""
        out: list[Path] = []
        for rel, _evt in _WATCH_MAP.items():
            target = entry.agent_root / rel
            if not target.exists():
                continue
            if target.is_file():
                out.append(target)
            elif target.is_dir():
                for sub in target.rglob("*"):
                    if sub.is_file() and not sub.name.startswith("."):
                        out.append(sub)
        return out

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, entry: _WatcherEntry, abs_path: Path) -> None:
        try:
            rel = abs_path.relative_to(entry.agent_root).as_posix()
        except ValueError:
            return
        event_type = match_event_type(rel)
        if event_type is None:
            return

        payload: dict[str, object] = {"path": rel}
        if event_type == "policy:bullets_updated":
            payload["bullets"] = self._render_policy_payload(entry)

        evt = FileChangeEvent(
            agent_id=entry.agent_id,
            event_type=event_type,
            path=rel,
            payload=payload,
        )
        await self._bus.emit(evt)

        emit_event(
            action="gateway.fs.changed",
            target=f"agent:{entry.agent_id}:{rel}",
            outcome="allow",
            extra={
                "agent_id": entry.agent_id,
                "path": rel,
                "event_type": event_type,
            },
        )

    def _render_policy_payload(self, entry: _WatcherEntry) -> list[dict[str, object]]:
        """Read + parse policy.md once at emit time."""
        try:
            content = read_file(
                scope="agent",
                agent_id=entry.agent_id,
                agent_root=entry.agent_root,
                rel_path="workspace/policy.md",
                caller_did="did:arc:gateway:fs_watcher",
            )
        except (FileNotFoundError, FileTooLargeError, PathTraversalError) as exc:
            logger.warning(
                "fs_watcher[%s]: policy.md unavailable: %s", entry.agent_id, exc
            )
            return []
        bullets = parse_bullets(content.content)
        return [
            {
                "id": b.id,
                "text": b.text,
                "score": b.score,
                "uses": b.uses,
                "reviewed": b.reviewed.isoformat() if b.reviewed else None,
                "created": b.created.isoformat() if b.created else None,
                "source": b.source,
                "retired": b.retired,
            }
            for b in bullets
        ]
