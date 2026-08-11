"""SPEC-021 Component C-001 — CapabilityLoader.

Discovers, validates, and registers capabilities from four scan roots
in precedence order (R-001):

  1. ``arcagent/builtins/capabilities/``  — package-internal
  2. ``~/.arc/capabilities/``              — global
  3. ``<agent_root>/capabilities/``        — per-agent
  4. ``<agent_root>/workspace/capabilities/`` — agent-authored

Per-file flow:

  1. Compute MD5+mtime; ``AstValidationCache`` hit skips re-validation.
  2. AST validate via :class:`AstValidator` — failure emits
     ``capability:registration_failed`` and is recorded in the
     reload diff.
  3. Apply the TOFU/signature policy gate.
  4. Trusted package code imports normally. Authored source is parsed for inert
     ``@tool`` metadata and represented by an ArcRun-isolated RPC proxy.
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
from pathlib import Path
from typing import Any

from arctrust import CapabilitySource, TofuDecision, TofuLayer

from arcagent.capabilities.capability_registry import (
    BackgroundTaskEntry,
    CapabilityRegistry,
    HookEntry,
    LifecycleEntry,
    ToolEntry,
)
from arcagent.capabilities.isolated_tool import (
    ArcRunIsolatedRunner,
    IsolatedRunner,
    make_isolated_execute,
    parse_authored_tools,
)
from arcagent.capabilities.reload_models import (
    CapabilityOutcome,
    PreparedReload,
)
from arcagent.capabilities.reload_models import (
    GateResult as _GateResult,
)
from arcagent.capabilities.reload_models import (
    ReloadDelta as _ReloadDelta,
)
from arcagent.capabilities.skill_validator import validate_skill_folder
from arcagent.capabilities.trust_backend import Ed25519TrustBackend, TrustBackend
from arcagent.tools._decorator import (
    BackgroundTaskMetadata,
    CapabilityClassMetadata,
    HookMetadata,
    ToolMetadata,
)
from arcagent.tools._dynamic_loader import (
    DEFAULT_IMPORT_POLICY,
    AstValidationCache,
    ImportPolicy,
)

# Roots that go through the AST validator + Sign/TOFU gate + isolated ArcRun
# execution. Every root an agent can write to is untrusted: ``workspace``
# (agent-authored), plus ``global`` (~/.arc/capabilities) and ``agent``
# (<agent_root>/capabilities), where a compromised agent can plant a ``.py``
# via bash and reload it. Only ``builtins`` / ``builtins-skills`` / ``module:*``
# — the harness's own shipped package code — are trusted. Operator-placed
# capabilities in global/agent must be signed or TOFU-approved to load above
# personal, which is correct for federal (nothing unsigned loads).
#
# The ``*-skills`` variants are the ``skills/`` subdir of each agent-writable
# capabilities root (where ``create_skill``/``update_skill`` write); a
# ``SKILL.md`` there is injected into the prompt (LLM01/ASI06), so it passes the
# same gate. ``builtins-skills`` is the ONLY skills root left trusted — shipped
# package code.
_UNTRUSTED_ROOTS: frozenset[str] = frozenset(
    {
        "workspace",
        "global",
        "agent",
        "workspace-skills",
        "global-skills",
        "agent-skills",
    }
)

#: Prefix of a per-extension root — ``extension:<name>`` and its ``-skills``
#: sibling (SPEC-062 REQ-281). A third-party bundle gets a root named after
#: itself, so no fixed set can enumerate them: a membership test alone would
#: call every extension root trusted by absence, exactly as ``module:*`` is.
EXTENSION_ROOT_PREFIX = "extension:"

_logger = logging.getLogger("arcagent.capabilities.capability_loader")


def is_untrusted_root(root_name: str) -> bool:
    """Return True if ``root_name`` must pass the AST validator + Sign/TOFU gate.

    The single load-time trust decision. Untrusted is the fixed set of
    agent-writable roots plus every per-extension root; ``builtins*`` and
    ``module:*`` — the harness's own shipped package code — stay trusted.
    """
    return root_name in _UNTRUSTED_ROOTS or root_name.startswith(EXTENSION_ROOT_PREFIX)


# Type alias for a (root_name, root_path) pair.
ScanRoot = tuple[str, Path]


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
        import_policy: ImportPolicy = DEFAULT_IMPORT_POLICY,
        tofu: TofuLayer | None = None,
        require_signature: bool = False,
        trusted_public_key: bytes | None = None,
        trust_backend: TrustBackend | None = None,
        spawn_background_tasks: bool = True,
        isolation_tier: str = "personal",
        isolated_runner: IsolatedRunner | None = None,
        ignored_python_paths: frozenset[Path] = frozenset(),
    ) -> None:
        self._scan_roots: list[ScanRoot] = list(scan_roots)
        self._registry = registry
        self._bus = bus
        self._audit_sink = audit_sink
        # Task #39: a read-only scan (arc agent tools/skills, arc ext inspect,
        # arcui's inventory seam) must not actually START a @background_task —
        # its body may depend on a live agent's module _runtime being
        # configured, which a throwaway scan never does. Default True keeps a
        # live agent's real startup/reload path (agent_lifecycle.py) spawning
        # exactly as before; only read-only callers pass False.
        self._spawn_background_tasks = spawn_background_tasks
        # SPEC-033 load-path Sign gate. ``tofu`` is the per-tier source-approval
        # policy; ``require_signature`` makes a valid detached signature the
        # floor (enterprise/federal); ``trusted_public_key`` pins self-authored
        # signatures to the agent's own DID key. All default off so a bare
        # library loader keeps pre-SPEC-033 behaviour — production wires them.
        self._tofu = tofu
        self._require_signature = require_signature
        self._trusted_public_key = trusted_public_key
        self._trust_backend: TrustBackend = trust_backend or Ed25519TrustBackend()
        self._known_tools: dict[str, str] = {}  # name → version
        self._known_skills: dict[str, str] = {}
        # Import policy for untrusted (agent-writable) roots (tier-resolved by
        # the caller). Default is the fail-closed enterprise blocklist so a bare
        # loader never silently allows all imports.
        self._import_policy = import_policy
        self._ast_cache = AstValidationCache(policy=import_policy)
        self._isolation_tier = isolation_tier
        # Construct the tier backend only when an authored tool is actually
        # present. Manifest-only extensions and skill-only roots must remain
        # inspectable/installable on hosts that cannot execute that tier.
        self._isolated_runner = isolated_runner
        self._ignored_python_paths = frozenset(path.resolve() for path in ignored_python_paths)

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

    async def prepare_reload(self) -> PreparedReload:
        """Scan into an isolated registry without changing live capabilities."""
        candidate_registry = CapabilityRegistry()
        candidate = CapabilityLoader(
            scan_roots=self._scan_roots,
            registry=candidate_registry,
            import_policy=self._import_policy,
            tofu=self._tofu,
            require_signature=self._require_signature,
            trusted_public_key=self._trusted_public_key,
            trust_backend=self._trust_backend,
            spawn_background_tasks=False,
            isolation_tier=self._isolation_tier,
            isolated_runner=self._isolated_runner,
            ignored_python_paths=self._ignored_python_paths,
        )
        prior_tools = dict(self._known_tools)
        prior_skills = dict(self._known_skills)
        delta = await candidate.scan_and_register()

        # Candidate registration starts from an empty registry. Recompute the
        # user-facing diff against the last committed scan, not against empty.
        next_tools = candidate._known_tools
        next_skills = candidate._known_skills
        delta.added = sorted(
            (set(next_tools) - set(prior_tools)) | (set(next_skills) - set(prior_skills))
        )
        delta.removed = sorted(
            (set(prior_tools) - set(next_tools)) | (set(prior_skills) - set(next_skills))
        )
        delta.replaced = sorted(
            (name, prior_tools[name], next_tools[name])
            for name in set(prior_tools) & set(next_tools)
            if prior_tools[name] != next_tools[name]
        ) + sorted(
            (name, prior_skills[name], next_skills[name])
            for name in set(prior_skills) & set(next_skills)
            if prior_skills[name] != next_skills[name]
        )
        return PreparedReload(
            registry=candidate_registry,
            delta=delta,
            known_tools=dict(next_tools),
            known_skills=dict(next_skills),
        )

    async def commit_reload(self, prepared: PreparedReload) -> None:
        """Atomically publish a previously validated candidate snapshot."""
        await self._registry.replace_from(prepared.registry)
        self._known_tools = dict(prepared.known_tools)
        self._known_skills = dict(prepared.known_skills)

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
                if entry.resolve() in self._ignored_python_paths:
                    continue
                await self._register_python_file(entry, root_name, delta, seen_tools)

    async def _register_python_file(
        self,
        path: Path,
        root_name: str,
        delta: _ReloadDelta,
        seen_tools: set[str],
    ) -> None:
        if is_untrusted_root(root_name):
            try:
                self._ast_cache.validate(path)
            except Exception as exc:  # reason: best-effort — record + continue
                detail = _short_error(exc)
                delta.errors.append((str(path), detail))
                self._record_tool_outcome(delta, path, root_name, "invalid", detail)
                await self._emit_registration_failed(path, "python", detail)
                return
            gate = await self._passes_trust_gate(path, path.stem, delta)
            if not gate.allowed:
                self._record_tool_outcome(delta, path, root_name, gate.status, gate.detail)
                return
            try:
                source = path.read_text(encoding="utf-8")
                authored_tools = parse_authored_tools(path)
            except Exception as exc:  # reason: fail closed before registration
                detail = _short_error(exc)
                delta.errors.append((str(path), detail))
                self._record_tool_outcome(delta, path, root_name, "invalid", detail)
                await self._emit_registration_failed(path, "python", detail)
                return
            for authored in authored_tools:
                execute = make_isolated_execute(
                    source=source,
                    function_name=authored.function_name,
                    runner=self._isolated_runner_for_tool(),
                )
                await self._dispatch_capability(
                    execute, authored.metadata, path, root_name, delta, seen_tools
                )
            return

        try:
            module = _load_module(path)
        except Exception as exc:  # reason: best-effort — record + continue
            detail = _short_error(exc)
            delta.errors.append((str(path), detail))
            self._record_tool_outcome(delta, path, root_name, "invalid", detail)
            await self._emit_registration_failed(path, "python", detail)
            return

        for value in vars(module).values():
            meta = getattr(value, "_arc_capability_meta", None)
            if meta is None:
                continue
            await self._dispatch_capability(value, meta, path, root_name, delta, seen_tools)

    def _isolated_runner_for_tool(self) -> IsolatedRunner:
        runner = self._isolated_runner
        if runner is None:
            runner = ArcRunIsolatedRunner(tier=self._isolation_tier)
            self._isolated_runner = runner
        return runner

    async def _passes_trust_gate(self, path: Path, name: str, delta: _ReloadDelta) -> _GateResult:
        """Fail-closed Sign gate for any agent-writable source (SPEC-033 B2/C2/D1).

        ``path`` is the signed artifact (a ``.py`` file or a skill's
        ``SKILL.md``); ``name`` is the capability/skill name TOFU keys on.
        Re-verifies the detached signature at LOAD, then consults
        :class:`TofuLayer`. Above personal a missing/invalid signature denies
        outright; TOFU governs first-sight (NEW_SIGHTING) and drift (DENY).
        Any evaluation error denies — nothing unsigned or un-adjudicated
        registers. When no policy is wired (bare library loader) the gate is a
        no-op, preserving pre-SPEC-033 behaviour.

        Requiring a signature implies a pinned key (SPEC-033 #6): without one,
        arctrust skips key-pinning and accepts any self-consistent signature, so
        an unpinned floor is no floor. Fail closed.
        """
        if self._tofu is None and not self._require_signature:
            return _GateResult(allowed=True)
        if self._require_signature and self._trusted_public_key is None:
            await self._deny_capability(path, "signature", "signature required but no pinned key")
            delta.errors.append((str(path), "signature: required but no pinned key — denied"))
            return _GateResult(
                allowed=False, status="unsigned", detail="required but no pinned key"
            )
        try:
            source_bytes = path.read_bytes()
            signed = self._trust_backend.verify(
                path, source_bytes, trusted_public_key=self._trusted_public_key
            )
            if self._require_signature and not signed:
                await self._deny_capability(path, "signature", "missing or invalid signature")
                delta.errors.append((str(path), "signature: unsigned/invalid — denied"))
                return _GateResult(
                    allowed=False, status="unsigned", detail="missing or invalid signature"
                )
            if self._tofu is None:
                return _GateResult(allowed=True)
            source_text = source_bytes.decode("utf-8")
            decision = self._tofu.evaluate(
                CapabilitySource(name=name, source=source_text, signed=signed)
            )
        except Exception as exc:  # reason: fail-closed — any evaluation error denies
            await self._deny_capability(path, "signature", _short_error(exc))
            delta.errors.append((str(path), f"trust-gate error: {_short_error(exc)}"))
            return _GateResult(allowed=False, status="error", detail=_short_error(exc))
        if decision is TofuDecision.ALLOW:
            return _GateResult(allowed=True)
        action = "new_sighting" if decision is TofuDecision.NEW_SIGHTING else "deny"
        await self._deny_capability(path, action, f"tofu decision {decision.value}")
        delta.errors.append((str(path), f"tofu: {decision.value}"))
        return _GateResult(
            allowed=False, status=decision.value, detail=f"tofu decision {decision.value}"
        )

    async def _deny_capability(self, path: Path, action: str, reason: str) -> None:
        """Emit the bus + audit event for a Sign-gate refusal."""
        payload = {"path": str(path), "reason": reason}
        event_name = f"capability:{action}"
        if self._bus is not None:
            await self._bus.emit(event=event_name, data=payload)
        self._audit(event_name, payload)

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
                ),
                spawn=self._spawn_background_tasks,
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
        delta.outcomes.append(
            CapabilityOutcome(
                kind="tool",
                name=meta.name,
                version=meta.version,
                description=meta.description,
                scan_root=root_name,
                source_path=str(path),
                status="loaded",
                status_detail="",
            )
        )

    @staticmethod
    def _record_tool_outcome(
        delta: _ReloadDelta, path: Path, root_name: str, status: str, detail: str
    ) -> None:
        """Record a refused/invalid ``.py`` tool candidate (never executed).

        Name falls back to the file stem and version/description are empty —
        the module never ran, so no ``@tool`` metadata exists.
        """
        delta.outcomes.append(
            CapabilityOutcome(
                kind="tool",
                name=path.stem,
                version="",
                description="",
                scan_root=root_name,
                source_path=str(path),
                status=status,
                status_detail=detail,
            )
        )

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
            delta.outcomes.append(
                CapabilityOutcome(
                    kind="skill",
                    name=folder.name,
                    version="",
                    description="",
                    scan_root=root_name,
                    source_path=str(skill_md),
                    status="invalid",
                    status_detail=detail,
                )
            )
            await self._emit_registration_failed(skill_md, "skill", detail)
            return
        entry = validation.entry
        # SKILL.md is injected into the agent prompt (LLM01/ASI06), so an
        # agent-writable skill folder passes the same Sign/TOFU gate as a .py.
        if is_untrusted_root(root_name):
            gate = await self._passes_trust_gate(skill_md, folder.name, delta)
            if not gate.allowed:
                delta.outcomes.append(
                    CapabilityOutcome(
                        kind="skill",
                        name=entry.name,
                        version=entry.version,
                        description=entry.description,
                        scan_root=root_name,
                        source_path=str(skill_md),
                        status=gate.status,
                        status_detail=gate.detail,
                    )
                )
                return
        for warning in validation.warnings:
            await self._emit_registration_warning(skill_md, warning.code, warning.detail)
        prior = self._known_skills.get(entry.name)
        result = await self._registry.register_skill(entry)
        self._known_skills[entry.name] = entry.version
        seen_skills.add(entry.name)
        if result.outcome == "added" and prior is None:
            delta.added.append(entry.name)
        elif result.outcome == "replaced" and result.previous_version is not None:
            delta.replaced.append((entry.name, result.previous_version, entry.version))
        delta.outcomes.append(
            CapabilityOutcome(
                kind="skill",
                name=entry.name,
                version=entry.version,
                description=entry.description,
                scan_root=root_name,
                source_path=str(skill_md),
                status="loaded",
                status_detail="",
            )
        )

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
            await self._registry.unregister(kind, name)  # type: ignore[arg-type]  # reason: kind is a runtime str narrowed by the caller's switch; registry expects Literal[...] and mypy can't see the narrowing
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
                await entry.instance.setup(None)  # type: ignore[attr-defined]  # reason: setup() is a duck-typed lifecycle hook; the @capability decorator attaches it but mypy sees instance as `object`
                entry.setup_done = True
                set_up.append(entry)
            except Exception as exc:  # reason: re-raise after log
                await self._emit_setup_failed(entry, exc)
                for done in reversed(set_up):
                    try:
                        await done.instance.teardown()  # type: ignore[attr-defined]  # reason: teardown() is a duck-typed lifecycle hook attached by @capability; mypy sees instance as `object`
                    except Exception:  # reason: fail-open — log + continue
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
                await entry.instance.teardown()  # type: ignore[attr-defined]  # reason: teardown() is a duck-typed lifecycle hook attached by @capability; mypy sees instance as `object`
            except Exception:  # reason: fail-open — log + continue
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
        except Exception:  # reason: fail-open — log + continue
            _logger.exception("loader audit sink raised; continuing")

    async def _topological_order(self) -> list[LifecycleEntry]:
        """Return capabilities in topological setup order over depends_on."""
        return _topological_sort(self._registry.lifecycle_entries())


# --- Helpers --------------------------------------------------------------


def _load_module(path: Path, *, restricted_builtins: dict[str, object] | None = None) -> Any:
    """Import a single .py file as a transient module.

    The module name is derived from the path so duplicate ``echo.py``
    files at different scan roots produce distinct module objects.

    When ``restricted_builtins`` is supplied (workspace-authored source),
    the module namespace is seeded with it BEFORE ``exec`` so the source
    runs under RESTRICTED_BUILTINS + the wrapped ``__import__`` instead of
    the full builtin surface — pre-seeding ``__builtins__`` makes ``exec``
    use it rather than injecting real builtins. First-party roots pass
    ``None`` and keep the trusted import path.

    Reads source + compile + exec directly instead of going through
    ``spec.loader.exec_module``. The latter consults importlib's .pyc
    bytecode cache, which is keyed by source mtime — and HFS+ / older
    APFS / some CI runners report 1-second mtime resolution. Two
    writes inside the same second produce identical mtimes, and the
    second reload silently serves the first version's bytecode. The
    explicit ``compile()`` path bypasses ``__pycache__`` entirely so
    reload is always honest about file content.
    """
    source = path.read_text(encoding="utf-8")
    module_name = f"_arc_cap_{path.stem}_{abs(hash(str(path))):x}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None:
        raise ImportError(f"could not build spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    if restricted_builtins is not None:
        module.__dict__["__builtins__"] = restricted_builtins
    try:
        code = compile(source, str(path), "exec")
        exec(code, module.__dict__)  # noqa: S102 — capability loader executes user code by design
    except Exception:  # reason: re-raise after log
        sys.modules.pop(module_name, None)
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


__all__ = [
    "EXTENSION_ROOT_PREFIX",
    "CapabilityLoader",
    "CapabilityOutcome",
    "ScanRoot",
    "is_untrusted_root",
]
