"""SPEC-021 — CapabilityRegistry (C-002).

Thread-safe, kind-discriminated registry. Five dicts (tools, skills,
hooks, tasks, capabilities) guarded by an :class:`aiorwlock.RWLock`.
Reader lock for queries (tool calls, prompt manifest); writer lock for
register / unregister.

Conflict resolution per kind (R-004):

  * **tools / skills / capability classes** — last-wins. New entry
    replaces existing; ``capability:replaced`` event emitted with the
    old version for diffing.
  * **hooks** — fan-out. Multiple handlers can subscribe to the same
    event; the registry keeps them in priority order. Caller (loader
    or bus) iterates the returned list.
  * **background_task** — drain-then-replace. On overwrite, the
    existing :class:`asyncio.Task` is cancelled and awaited (catching
    ``CancelledError``) before the new task starts.

The XML manifest produced by :meth:`format_for_prompt` is cached as a
single string (interned identity matters for the prompt-cache layer
above) and invalidated on every successful mutation.

The registry does NOT call ``setup()`` / ``teardown()`` on capability
classes — that's the loader's job (C-001) where dependency ordering is
known. The registry only stores entries.
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import aiorwlock

from arcagent.tools._decorator import (
    BackgroundTaskMetadata,
    CapabilityClassMetadata,
    HookMetadata,
    ToolMetadata,
)

_logger = logging.getLogger("arcagent.core.capability_registry")

Kind = Literal["tool", "skill", "hook", "background_task", "capability"]


# --- Entry types ---------------------------------------------------------


@dataclass(frozen=True)
class ToolEntry:
    """A registered ``@tool``-decorated callable plus its provenance."""

    meta: ToolMetadata
    execute: Callable[..., Awaitable[Any]]
    source_path: Path
    scan_root: str  # "builtins" | "global" | "agent" | "workspace"


@dataclass(frozen=True)
class SkillEntry:
    """A registered skill folder plus the SKILL.md location."""

    name: str
    version: str
    description: str
    triggers: tuple[str, ...]
    tools: tuple[str, ...]
    location: Path
    scan_root: str
    model_hint: str | None = None


@dataclass(frozen=True)
class HookEntry:
    """A registered ``@hook``-decorated bus subscriber."""

    meta: HookMetadata
    handler: Callable[..., Awaitable[None]]
    source_path: Path
    scan_root: str


@dataclass
class BackgroundTaskEntry:
    """A registered ``@background_task``.

    ``task`` is set after :meth:`CapabilityRegistry.register_task`
    spawns the runner. On replace, the registry cancels and awaits
    the old task before starting a new one (R-062).
    """

    meta: BackgroundTaskMetadata
    fn: Callable[..., Coroutine[Any, Any, None]]
    source_path: Path
    scan_root: str
    task: asyncio.Task[None] | None = None


@dataclass
class LifecycleEntry:
    """A registered ``@capability`` class instance.

    ``setup_done`` is flipped by the loader after a successful
    ``setup(ctx)`` call so teardown ordering can skip not-yet-set-up
    instances.
    """

    meta: CapabilityClassMetadata
    instance: object
    source_path: Path
    scan_root: str
    setup_done: bool = False


@dataclass(frozen=True)
class RegisterResult:
    """Outcome of a register call.

    ``previous_version`` is set on ``"replaced"`` so the loader can
    render the ``v1.0.0 → v1.1.0`` segment of the reload diff string
    (R-005).
    """

    outcome: Literal["added", "replaced"]
    previous_version: str | None = None


# --- Registry -------------------------------------------------------------


class CapabilityRegistry:
    """In-memory capability store guarded by an aiorwlock.

    A single :class:`CapabilityRegistry` is owned by an
    :class:`Agent`; the loader (C-001) feeds entries; arcrun's tool
    execution path queries it via :meth:`to_arcrun_tools`; the prompt
    assembly subscriber pulls XML via :meth:`format_for_prompt`.

    All public methods are async because the lock is async. Reader
    methods (``get_*``, ``format_for_prompt``, ``to_arcrun_tools``)
    take the reader lock — many can run concurrently. Writer methods
    (``register_*``, ``unregister``) take the writer lock — exclusive.
    """

    def __init__(
        self,
        *,
        bus: Any | None = None,
        audit_sink: Any | None = None,
        agent_did: str = "",
        tier: str = "personal",
    ) -> None:
        self._lock = aiorwlock.RWLock()
        self._tools: dict[str, ToolEntry] = {}
        self._skills: dict[str, SkillEntry] = {}
        self._hooks: dict[str, list[HookEntry]] = {}
        self._tasks: dict[str, BackgroundTaskEntry] = {}
        self._capabilities: dict[str, LifecycleEntry] = {}
        self._prompt_cache: str | None = None
        self._bus = bus
        self._audit_sink = audit_sink
        self._agent_did = agent_did
        self._tier = tier

    # --- Tools ------------------------------------------------------------

    async def register_tool(self, entry: ToolEntry) -> RegisterResult:
        async with self._lock.writer:
            existing = self._tools.get(entry.meta.name)
            self._tools[entry.meta.name] = entry
            self._invalidate_cache()
        result = self._diff_result(existing.meta.version if existing else None)
        await self._emit_lifecycle(result, entry)
        return result

    async def get_tool(self, name: str) -> ToolEntry | None:
        async with self._lock.reader:
            return self._tools.get(name)

    # --- Skills -----------------------------------------------------------

    async def register_skill(self, entry: SkillEntry) -> RegisterResult:
        async with self._lock.writer:
            existing = self._skills.get(entry.name)
            self._skills[entry.name] = entry
            self._invalidate_cache()
        result = self._diff_result(existing.version if existing else None)
        await self._emit_lifecycle(result, entry)
        return result

    async def get_skill(self, name: str) -> SkillEntry | None:
        async with self._lock.reader:
            return self._skills.get(name)

    # --- Hooks ------------------------------------------------------------

    async def register_hook(self, entry: HookEntry) -> RegisterResult:
        """Hooks fan out — never replace by name. Always added.

        Multiple ``@hook(event="X")`` registrations all run on event
        emission, ordered by ``priority``. The registry keeps the
        per-event list sorted so callers don't have to.
        """
        async with self._lock.writer:
            event_hooks = self._hooks.setdefault(entry.meta.event, [])
            event_hooks.append(entry)
            event_hooks.sort(key=lambda h: h.meta.priority)
        result = RegisterResult(outcome="added")
        await self._emit_lifecycle(result, entry)
        return result

    async def get_hooks(self, event: str) -> list[HookEntry]:
        async with self._lock.reader:
            return list(self._hooks.get(event, ()))

    # --- Background tasks -------------------------------------------------

    async def register_task(self, entry: BackgroundTaskEntry) -> RegisterResult:
        """Drain-then-replace per R-062.

        If a task with the same name already runs, cancel it, await
        completion (swallowing :class:`asyncio.CancelledError`), then
        spawn the new task. No overlap.
        """
        old: BackgroundTaskEntry | None = None
        async with self._lock.writer:
            old = self._tasks.get(entry.meta.name)
            self._tasks[entry.meta.name] = entry

        if old is not None:
            await _drain_task(old.task)

        entry.task = asyncio.create_task(entry.fn(None), name=f"capability_task:{entry.meta.name}")
        result = self._diff_result(
            old.meta.name if old else None  # version not on task meta
        )
        await self._emit_lifecycle(result, entry)
        return result

    async def get_task(self, name: str) -> BackgroundTaskEntry | None:
        async with self._lock.reader:
            return self._tasks.get(name)

    # --- Capability classes ----------------------------------------------

    async def register_capability(self, entry: LifecycleEntry) -> RegisterResult:
        async with self._lock.writer:
            existing = self._capabilities.get(entry.meta.name)
            self._capabilities[entry.meta.name] = entry
        result = self._diff_result(
            "1.0.0" if existing else None  # capability classes
            # don't carry a version field today; placeholder so a
            # diff can render "replaced" without crashing.
        )
        await self._emit_lifecycle(result, entry)
        return result

    async def get_capability(self, name: str) -> LifecycleEntry | None:
        async with self._lock.reader:
            return self._capabilities.get(name)

    # --- Unregister -------------------------------------------------------

    async def unregister(self, kind: Kind, name: str) -> None:
        """Remove an entry by kind+name. No-op if absent.

        Background-task drain happens outside the writer lock so a
        slow ``await`` on a cancelling task does not block readers.
        """
        drainable_task: asyncio.Task[None] | None = None
        removed_version: str | None = None
        async with self._lock.writer:
            if kind == "tool":
                removed_t = self._tools.pop(name, None)
                if removed_t is not None:
                    removed_version = removed_t.meta.version
                    self._invalidate_cache()
            elif kind == "skill":
                removed_s = self._skills.pop(name, None)
                if removed_s is not None:
                    removed_version = removed_s.version
                    self._invalidate_cache()
            elif kind == "background_task":
                removed_b = self._tasks.pop(name, None)
                if removed_b is not None:
                    removed_version = "1.0.0"
                    drainable_task = removed_b.task
            elif kind == "capability":
                removed_c = self._capabilities.pop(name, None)
                if removed_c is not None:
                    removed_version = "1.0.0"

        if removed_version is None:
            return
        await _drain_task(drainable_task)
        await self._emit_removed(kind, name, removed_version)

    # --- Manifest XML -----------------------------------------------------

    async def format_for_prompt(self) -> str:
        """Build the XML manifest for system-prompt injection (R-020).

        Cached: identical content returns the same string until any
        register/unregister mutation invalidates the cache. The agent's
        prompt-cache layer uses identity comparison, so this matters.
        """
        async with self._lock.reader:
            if self._prompt_cache is not None:
                return self._prompt_cache
            rendered = self._render_manifest_locked()
            self._prompt_cache = rendered
            return rendered

    def _render_manifest_locked(self) -> str:
        """Build manifest XML; caller holds reader (or writer) lock."""
        tools_el = ET.Element("available-tools")
        for name in sorted(self._tools):
            entry = self._tools[name]
            tool_el = ET.SubElement(
                tools_el,
                "tool",
                attrib={
                    "name": entry.meta.name,
                    "version": entry.meta.version,
                    "classification": entry.meta.classification,
                },
            )
            ET.SubElement(tool_el, "description").text = entry.meta.description
            if entry.meta.when_to_use:
                ET.SubElement(tool_el, "when-to-use").text = entry.meta.when_to_use
            if entry.meta.requires_skill:
                ET.SubElement(tool_el, "requires-skill").text = entry.meta.requires_skill

        skills_el = ET.Element("available-skills")
        for sname in sorted(self._skills):
            sentry = self._skills[sname]
            skill_el = ET.SubElement(
                skills_el,
                "skill",
                attrib={
                    "name": sentry.name,
                    "version": sentry.version,
                    "location": str(sentry.location),
                },
            )
            ET.SubElement(skill_el, "description").text = sentry.description
            if sentry.triggers:
                ET.SubElement(skill_el, "triggers").text = ", ".join(sentry.triggers)
            if sentry.tools:
                ET.SubElement(skill_el, "tools").text = ", ".join(sentry.tools)

        # ElementTree.tostring returns bytes by default; we want str.
        tools_xml = ET.tostring(tools_el, encoding="unicode")
        skills_xml = ET.tostring(skills_el, encoding="unicode")
        return f"{tools_xml}\n{skills_xml}"

    def _invalidate_cache(self) -> None:
        self._prompt_cache = None

    # --- ArcRun tool list -------------------------------------------------

    async def to_arcrun_tools(self) -> list[Any]:
        """Build a list of :class:`arcrun.types.Tool` for the runtime loop.

        ``parallel_safe`` follows the tool's classification:
        ``read_only`` → True, ``state_modifying`` → False (matches the
        existing :class:`ToolRegistry` convention).

        Execute is wrapped to forward kwargs and stringify the result —
        the policy / audit layers wrap this further at agent glue time
        (existing pattern — see C-008).
        """
        from arcrun.types import Tool as ArcRunTool

        async with self._lock.reader:
            entries = list(self._tools.values())

        result: list[Any] = []
        for entry in entries:
            wrapped = _wrap_for_arcrun(entry.execute)
            result.append(
                ArcRunTool(
                    name=entry.meta.name,
                    description=entry.meta.description,
                    input_schema=entry.meta.input_schema,
                    execute=wrapped,
                    timeout_seconds=None,
                    parallel_safe=(entry.meta.classification == "read_only"),
                )
            )
        return result

    # --- Lifecycle event emission ---------------------------------------

    async def _emit_lifecycle(
        self,
        result: RegisterResult,
        entry: ToolEntry | SkillEntry | HookEntry | BackgroundTaskEntry | LifecycleEntry,
    ) -> None:
        """Emit ``capability:added`` / ``capability:replaced``.

        Two channels — module bus (in-process subscribers) and the
        audit sink (NIST AU-2 tamper-evident log). Both are optional;
        absence of either is silent.
        """
        event_name = "capability:replaced" if result.outcome == "replaced" else "capability:added"
        payload = _payload_for(entry, result)
        if self._bus is not None:
            await self._bus.emit(event=event_name, data=payload)
        self._audit(event_name, payload)

    async def _emit_removed(self, kind: Kind, name: str, version: str) -> None:
        payload = {"kind": kind, "name": name, "version": version}
        if self._bus is not None:
            await self._bus.emit(event="capability:removed", data=payload)
        self._audit("capability:removed", payload)

    def _audit(self, event_name: str, payload: dict[str, Any]) -> None:
        """Emit a structured AuditEvent via the configured sink (if any).

        The sink may be any object exposing ``emit(event)``; we don't
        import :mod:`arctrust.audit` types here to keep the registry
        loosely coupled. The agent wires the concrete sink at startup.
        """
        if self._audit_sink is None:
            return
        try:
            from arctrust.audit import AuditEvent
        except ImportError:
            _logger.debug("arctrust.audit unavailable — skipping audit emission")
            return
        try:
            event = AuditEvent(
                actor_did=self._agent_did or "did:arc:capability-loader",
                action=event_name.replace(":", "."),
                target=str(payload.get("name", "")),
                outcome=payload.get("outcome", "ok"),
                tier=self._tier,
                extra=payload,
            )
            self._audit_sink.emit(event)
        except Exception:
            _logger.exception("audit sink raised; continuing")

    @staticmethod
    def _diff_result(prior_version: str | None) -> RegisterResult:
        if prior_version is None:
            return RegisterResult(outcome="added")
        return RegisterResult(outcome="replaced", previous_version=prior_version)


# --- Helpers --------------------------------------------------------------


async def _drain_task(task: asyncio.Task[None] | None) -> None:
    """Cancel and await a task, swallowing CancelledError (R-062)."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        _logger.exception("background task raised during drain; continuing")


def _payload_for(
    entry: ToolEntry | SkillEntry | HookEntry | BackgroundTaskEntry | LifecycleEntry,
    result: RegisterResult,
) -> dict[str, Any]:
    """Build a structured payload for ``capability:added``/``replaced``."""
    base: dict[str, Any] = {
        "name": _name_of(entry),
        "kind": _kind_of(entry),
        "source_path": str(getattr(entry, "source_path", getattr(entry, "location", ""))),
        "scan_root": entry.scan_root,
    }
    if result.outcome == "replaced" and result.previous_version is not None:
        base["previous_version"] = result.previous_version
    return base


def _name_of(entry: object) -> str:
    if isinstance(entry, SkillEntry):
        return entry.name
    meta = getattr(entry, "meta", None)
    return getattr(meta, "name", "") if meta is not None else ""


def _kind_of(entry: object) -> str:
    if isinstance(entry, ToolEntry):
        return "tool"
    if isinstance(entry, SkillEntry):
        return "skill"
    if isinstance(entry, HookEntry):
        return "hook"
    if isinstance(entry, BackgroundTaskEntry):
        return "background_task"
    if isinstance(entry, LifecycleEntry):
        return "capability"
    return "unknown"


def _wrap_for_arcrun(
    execute: Callable[..., Awaitable[Any]],
) -> Callable[[dict[str, Any], Any], Awaitable[str]]:
    """Adapt a kwargs-style coroutine to arcrun's ``(args, ctx) -> str``."""

    async def arcrun_execute(args: dict[str, Any], ctx: Any) -> str:
        del ctx  # arcrun's tool ctx is unused at this layer
        result = await execute(**args)
        return str(result)

    return arcrun_execute


__all__ = [
    "BackgroundTaskEntry",
    "CapabilityRegistry",
    "HookEntry",
    "Kind",
    "LifecycleEntry",
    "RegisterResult",
    "SkillEntry",
    "ToolEntry",
]
