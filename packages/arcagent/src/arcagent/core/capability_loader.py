"""SPEC-021 Component C-001 — CapabilityLoader.

Discovers, validates, and registers capabilities from four scan roots
in precedence order (R-001):

  1. ``arcagent/builtins/capabilities/``  — package-internal
  2. ``~/.arc/capabilities/``              — global
  3. ``<agent_root>/capabilities/``        — per-agent
  4. ``<agent_root>/workspace/.capabilities/`` — agent-authored

Per-file flow:

  1. Compute MD5+mtime; ``AstValidationCache`` hit skips re-validation.
  2. AST validate via :class:`AstValidator` — failure emits
     ``capability:registration_failed`` and is recorded in the
     reload diff.
  3. (Future) TOFU policy gate; OS sandbox for self-executing code.
  4. Import as a transient module; find decorated callables / classes.
  5. Hand to :class:`CapabilityRegistry` (kind-aware register).

The loader's :meth:`reload` returns the human-readable diff string
specified by R-005:

  * Nominal — single line: ``reload: +N added (...), ~M replaced
    (... v→v), -K removed (...), 0 errors``
  * With errors — multi-line; the head line is the same and each
    error appears on its own indented line.

Lifecycle (R-061): :meth:`start_lifecycles` runs ``setup(ctx)`` on each
``@capability`` class in topological order over ``depends_on``. If a
``setup()`` raises, already-set-up siblings are torn down in reverse
order before re-raising. :meth:`shutdown` does the symmetric
reverse-topo teardown.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcagent.core.capability_registry import (
    BackgroundTaskEntry,
    CapabilityRegistry,
    HookEntry,
    LifecycleEntry,
    ToolEntry,
)
from arcagent.core.skill_validator import validate_skill_folder
from arcagent.tools._decorator import (
    BackgroundTaskMetadata,
    CapabilityClassMetadata,
    HookMetadata,
    ToolMetadata,
)
from arcagent.tools._dynamic_loader import AstValidationCache

# Roots that go through the AST validator + (future) TOFU + sandbox.
# Trusted roots (builtins/global/agent) ship with the agent or are
# operator-curated; their authors are responsible for safety.
_UNTRUSTED_ROOTS: frozenset[str] = frozenset({"workspace"})

_logger = logging.getLogger("arcagent.core.capability_loader")

# Type alias for a (root_name, root_path) pair.
ScanRoot = tuple[str, Path]


@dataclass
class _ReloadDelta:
    """Tracks adds/removes/replaces during a single reload pass."""

    added: list[str] = field(default_factory=list)
    replaced: list[tuple[str, str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    def render(self) -> str:
        """Produce the R-005 diff string."""
        added_seg = self._segment("added", self.added)
        replaced_seg = self._render_replaced()
        removed_seg = self._segment("removed", self.removed)
        error_count = len(self.errors)
        head = (
            f"reload: +{len(self.added)} added{added_seg}, "
            f"~{len(self.replaced)} replaced{replaced_seg}, "
            f"-{len(self.removed)} removed{removed_seg}, "
            f"{error_count} {'error' if error_count == 1 else 'errors'}"
        )
        if error_count == 0:
            return head
        lines = [head]
        lines.extend(f"  - {path}: {detail}" for path, detail in self.errors)
        return "\n".join(lines)

    @staticmethod
    def _segment(_label: str, names: list[str]) -> str:
        if not names:
            return ""
        return " (" + ", ".join(names) + ")"

    def _render_replaced(self) -> str:
        if not self.replaced:
            return ""
        return " (" + ", ".join(f"{name} {old}→{new}" for name, old, new in self.replaced) + ")"


class CapabilityLoader:
    """Scan four roots, register decorated capabilities into the registry.

    The loader is stateful — it remembers the names registered on the
    last successful pass so :meth:`reload` can compute removals
    (capabilities present last time but not this time).
    """

    def __init__(
        self,
        *,
        scan_roots: Iterable[ScanRoot],
        registry: CapabilityRegistry,
        bus: Any | None = None,
        audit_sink: Any | None = None,
    ) -> None:
        self._scan_roots: list[ScanRoot] = list(scan_roots)
        self._registry = registry
        self._bus = bus
        self._audit_sink = audit_sink
        self._known_tools: dict[str, str] = {}  # name → version
        self._known_skills: dict[str, str] = {}
        self._ast_cache = AstValidationCache()

    async def scan_and_register(self) -> _ReloadDelta:
        """Walk scan roots in precedence order; register everything found."""
        delta = _ReloadDelta()
        seen_tools: set[str] = set()
        seen_skills: set[str] = set()

        for root_name, root_path in self._scan_roots:
            if not root_path.is_dir():
                continue
            await self._scan_root(root_name, root_path, delta, seen_tools, seen_skills)

        # Removals: anything we knew about last pass but didn't see now.
        await self._remove_unseen("tool", self._known_tools, seen_tools, delta)
        await self._remove_unseen("skill", self._known_skills, seen_skills, delta)

        # Update known sets to current pass.
        self._known_tools = {
            name: ver for name, ver in self._known_tools.items() if name in seen_tools
        }
        self._known_skills = {
            name: ver for name, ver in self._known_skills.items() if name in seen_skills
        }
        return delta

    async def reload(self) -> str:
        """Run :meth:`scan_and_register`; return R-005 diff string."""
        delta = await self.scan_and_register()
        return delta.render()

    # --- Discovery ---------------------------------------------------------

    async def _scan_root(
        self,
        root_name: str,
        root_path: Path,
        delta: _ReloadDelta,
        seen_tools: set[str],
        seen_skills: set[str],
    ) -> None:
        for entry in sorted(root_path.iterdir()):
            if entry.is_dir() and (entry / "SKILL.md").exists():
                await self._register_skill_folder(entry, root_name, delta, seen_skills)
                continue
            if entry.is_file() and entry.suffix == ".py":
                await self._register_python_file(entry, root_name, delta, seen_tools)

    async def _register_python_file(
        self,
        path: Path,
        root_name: str,
        delta: _ReloadDelta,
        seen_tools: set[str],
    ) -> None:
        if root_name in _UNTRUSTED_ROOTS:
            try:
                self._ast_cache.validate(path)
            except Exception as exc:
                detail = _short_error(exc)
                delta.errors.append((str(path), detail))
                await self._emit_registration_failed(path, "python", detail)
                return
        try:
            module = _load_module(path)
        except Exception as exc:
            detail = _short_error(exc)
            delta.errors.append((str(path), detail))
            await self._emit_registration_failed(path, "python", detail)
            return

        for value in vars(module).values():
            meta = getattr(value, "_arc_capability_meta", None)
            if meta is None:
                continue
            await self._dispatch_capability(value, meta, path, root_name, delta, seen_tools)

    async def _dispatch_capability(
        self,
        value: Any,
        meta: Any,
        path: Path,
        root_name: str,
        delta: _ReloadDelta,
        seen_tools: set[str],
    ) -> None:
        """Dispatch a stamped value to the registry by ``meta.kind``."""
        if isinstance(meta, ToolMetadata):
            await self._register_tool(value, meta, path, root_name, delta)
            seen_tools.add(meta.name)
        elif isinstance(meta, HookMetadata):
            await self._registry.register_hook(
                HookEntry(
                    meta=meta,
                    handler=value,
                    source_path=path,
                    scan_root=root_name,
                )
            )
        elif isinstance(meta, BackgroundTaskMetadata):
            await self._registry.register_task(
                BackgroundTaskEntry(
                    meta=meta,
                    fn=value,
                    source_path=path,
                    scan_root=root_name,
                )
            )
        elif isinstance(meta, CapabilityClassMetadata):
            await self._registry.register_capability(
                LifecycleEntry(
                    meta=meta,
                    instance=value(),
                    source_path=path,
                    scan_root=root_name,
                )
            )

    async def _register_tool(
        self,
        execute: Any,
        meta: ToolMetadata,
        path: Path,
        root_name: str,
        delta: _ReloadDelta,
    ) -> None:
        prior_version = self._known_tools.get(meta.name)
        result = await self._registry.register_tool(
            ToolEntry(
                meta=meta,
                execute=execute,
                source_path=path,
                scan_root=root_name,
            )
        )
        self._known_tools[meta.name] = meta.version
        if result.outcome == "added" and prior_version is None:
            delta.added.append(meta.name)
        elif result.outcome == "replaced" and result.previous_version is not None:
            delta.replaced.append((meta.name, result.previous_version, meta.version))

    async def _register_skill_folder(
        self,
        folder: Path,
        root_name: str,
        delta: _ReloadDelta,
        seen_skills: set[str],
    ) -> None:
        skill_md = folder / "SKILL.md"
        validation = validate_skill_folder(folder, root_name)
        if not validation.ok or validation.entry is None:
            detail = "; ".join(f"{e.code}: {e.detail}" for e in validation.errors)
            delta.errors.append((str(skill_md), detail))
            await self._emit_registration_failed(skill_md, "skill", detail)
            return
        for warning in validation.warnings:
            await self._emit_registration_warning(skill_md, warning.code, warning.detail)
        entry = validation.entry
        prior = self._known_skills.get(entry.name)
        result = await self._registry.register_skill(entry)
        self._known_skills[entry.name] = entry.version
        seen_skills.add(entry.name)
        if result.outcome == "added" and prior is None:
            delta.added.append(entry.name)
        elif result.outcome == "replaced" and result.previous_version is not None:
            delta.replaced.append((entry.name, result.previous_version, entry.version))

    async def _remove_unseen(
        self,
        kind: str,
        known: dict[str, str],
        seen: set[str],
        delta: _ReloadDelta,
    ) -> None:
        for name in list(known):
            if name in seen:
                continue
            # Cast kind to the registry's Literal at the call boundary.
            await self._registry.unregister(kind, name)  # type: ignore[arg-type]
            delta.removed.append(name)
            del known[name]

    # --- Lifecycle ---------------------------------------------------------

    async def start_lifecycles(self) -> None:
        """Topologically run setup() on each registered capability class.

        On exception, emit ``capability:setup_failed`` for the failing
        instance, then tear down already-set-up siblings in reverse
        order before re-raising.
        """
        ordered = await self._topological_order()
        set_up: list[LifecycleEntry] = []
        for entry in ordered:
            try:
                await entry.instance.setup(None)  # type: ignore[attr-defined]
                entry.setup_done = True
                set_up.append(entry)
            except Exception as exc:
                await self._emit_setup_failed(entry, exc)
                for done in reversed(set_up):
                    try:
                        await done.instance.teardown()  # type: ignore[attr-defined]
                    except Exception:
                        _logger.exception(
                            "teardown raised during rollback for %s",
                            done.meta.name,
                        )
                raise

    async def shutdown(self) -> None:
        """Reverse-topological teardown of all set-up capabilities."""
        ordered = await self._topological_order()
        for entry in reversed(ordered):
            if not entry.setup_done:
                continue
            try:
                await entry.instance.teardown()  # type: ignore[attr-defined]
            except Exception:
                _logger.exception("teardown raised during shutdown for %s", entry.meta.name)

    # --- Bus + audit emission ---------------------------------------------

    async def _emit_registration_failed(self, path: Path, kind: str, detail: str) -> None:
        payload = {"path": str(path), "kind": kind, "reason": detail}
        if self._bus is not None:
            await self._bus.emit(event="capability:registration_failed", data=payload)
        self._audit("capability:registration_failed", payload)

    async def _emit_registration_warning(self, path: Path, code: str, detail: str) -> None:
        payload = {"path": str(path), "code": code, "detail": detail}
        if self._bus is not None:
            await self._bus.emit(event="capability:registration_warning", data=payload)
        self._audit("capability:registration_warning", payload)

    async def _emit_setup_failed(self, entry: LifecycleEntry, exc: BaseException) -> None:
        payload = {
            "name": entry.meta.name,
            "kind": "capability",
            "exception_type": type(exc).__name__,
            "exception_msg": str(exc),
        }
        if self._bus is not None:
            await self._bus.emit(event="capability:setup_failed", data=payload)
        self._audit("capability:setup_failed", payload)

    def _audit(self, event_name: str, payload: dict[str, Any]) -> None:
        if self._audit_sink is None:
            return
        try:
            from arctrust.audit import AuditEvent
        except ImportError:
            return
        try:
            event = AuditEvent(
                actor_did="did:arc:capability-loader",
                action=event_name.replace(":", "."),
                target=str(payload.get("name") or payload.get("path") or ""),
                outcome="error",
                extra=payload,
            )
            self._audit_sink.emit(event)
        except Exception:
            _logger.exception("loader audit sink raised; continuing")

    async def _topological_order(self) -> list[LifecycleEntry]:
        """Return capabilities in topological setup order over depends_on."""
        # Pull entries via a fresh reader-locked view.
        # The registry exposes capabilities by name; iterate via known
        # names from the underlying dict. We read under the lock.
        async with self._registry._lock.reader:
            entries = dict(self._registry._capabilities)
        return _topological_sort(entries)


# --- Helpers --------------------------------------------------------------


def _load_module(path: Path) -> Any:
    """Import a single .py file as a transient module.

    The module name is derived from the path so duplicate ``echo.py``
    files at different scan roots produce distinct module objects.
    """
    spec = importlib.util.spec_from_file_location(
        f"_arc_cap_{path.stem}_{abs(hash(str(path))):x}", path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _short_error(exc: BaseException) -> str:
    """Compact one-line description of an exception for the diff string."""
    return f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()


def _topological_sort(
    entries: dict[str, LifecycleEntry],
) -> list[LifecycleEntry]:
    """Topologically order capabilities by their declared depends_on.

    Cycles are flagged loudly — capability dependency cycles are a
    config bug, not a recoverable error.
    """
    visited: set[str] = set()
    visiting: set[str] = set()
    out: list[LifecycleEntry] = []

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise ValueError(f"capability dependency cycle through {name!r}")
        visiting.add(name)
        entry = entries.get(name)
        if entry is not None:
            for dep in entry.meta.depends_on:
                if dep in entries:
                    visit(dep)
            out.append(entry)
        visiting.discard(name)
        visited.add(name)

    for name in entries:
        visit(name)
    return out


__all__ = ["CapabilityLoader", "ScanRoot"]
