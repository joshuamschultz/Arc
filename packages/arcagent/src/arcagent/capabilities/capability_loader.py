"""SPEC-021 Component C-001 — CapabilityLoader.

Discovers, validates, and registers capabilities from four scan roots
in precedence order (R-001):

  1. ``arcagent/builtins/capabilities/``  — package-internal
  2. ``~/.arc/capabilities/``              — global
  3. ``<agent_root>/capabilities/``        — per-agent
  4. ``<agent_root>/workspace/capabilities/`` — agent-authored

Per-file flow, selected by the root's :class:`RootTrust` class:

  1. AST validate via :class:`AstValidator` (UNTRUSTED only) — failure emits
     ``capability:registration_failed`` and is recorded in the reload diff.
     ``AstValidationCache`` skips re-validation on an MD5+mtime hit.
  2. Apply the TOFU/signature policy gate (UNTRUSTED and VERIFIED).
  3. TRUSTED and VERIFIED source imports normally. UNTRUSTED source is parsed
     for inert ``@tool`` metadata and represented by an ArcRun-isolated RPC
     proxy — it never enters this process.
  4. Hand to :class:`CapabilityRegistry` (kind-aware register).

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
from enum import StrEnum
from pathlib import Path, PurePosixPath
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

#: Roots that are the harness's own shipped package code, inside the wheel and
#: unreachable by any write an agent or an operator makes at runtime. A closed
#: set, and the ONLY closed set here: everything not named below is classified by
#: rule, so a scan root nobody thought about cannot become trusted by falling off
#: the end of a chain of tests. ``builtins-skills`` is the one skills root left
#: trusted — a ``SKILL.md`` anywhere else is injected into the prompt
#: (LLM01/ASI06) from a directory somebody can write to, so it passes the gate.
_TRUSTED_ROOTS: frozenset[str] = frozenset({"builtins", "builtins-skills"})

#: Prefix of a per-extension root — ``extension:<name>`` and its ``-skills``
#: sibling (SPEC-062 REQ-281). A third-party bundle gets a root named after
#: itself, so no fixed set can enumerate them: a membership test alone would
#: call every extension root trusted by absence.
EXTENSION_ROOT_PREFIX = "extension:"

#: Prefix of a per-module root — ``module:<name>`` (SPEC-066). A module arrives
#: as a signed bundle materialized into the deployment module root, which is a
#: directory an install writes into rather than wheel content, so it is verified
#: at load like any other non-package source.
MODULE_ROOT_PREFIX = "module:"

#: The one file whose presence makes a folder a skill. The FOLDER name is what
#: TOFU pins, which is why the manifest name is needed to derive a pin name.
SKILL_MANIFEST = "SKILL.md"

_logger = logging.getLogger("arcagent.capabilities.capability_loader")


class RootTrust(StrEnum):
    """How much of the load-time gate a scan root's contents must pass.

    Three classes, because two collapse a real distinction. The gate does two
    separable jobs — *prove who wrote these bytes* (signature + TOFU) and
    *contain what they may do* (AST import allowlist + ArcRun-isolated
    execution) — and module capabilities need the first without the second.
    """

    TRUSTED = "trusted"
    """Shipped package code. No proof asked for, nothing contained."""

    VERIFIED = "verified"
    """First-party code delivered through a signed bundle: proof required,
    execution NOT contained. Containment exists to hold code the MODEL wrote;
    applying it to a module means ``@hook`` / ``@background_task`` /
    ``@capability`` never register and the stdlib a module legitimately imports
    is blocked — measured at 17 of 18 modules registering zero tools."""

    UNTRUSTED = "untrusted"
    """Anything an agent can write: proof required AND execution contained."""


def root_trust(root_name: str) -> RootTrust:
    """Classify ``root_name`` into its load-time trust class.

    The single load-time trust decision. Every root lands in a named class, and
    the default is the strictest one: ``workspace`` (agent-authored), ``global``
    (``~/.arc/capabilities``), ``agent`` (``<agent_root>/capabilities``), their
    ``*-skills`` siblings, and every ``extension:<name>`` root are all places a
    compromised agent or a third party can put a ``.py``, and so is any root
    nobody has classified. Forgetting to classify one costs it privilege rather
    than granting it.
    """
    if root_name in _TRUSTED_ROOTS:
        return RootTrust.TRUSTED
    if root_name.startswith(MODULE_ROOT_PREFIX):
        return RootTrust.VERIFIED
    return RootTrust.UNTRUSTED


def is_untrusted_root(root_name: str) -> bool:
    """Return True if ``root_name`` must pass the AST validator + Sign/TOFU gate.

    Kept as the name every other surface (``extension.loader``, the arcui
    inventory) asks the question by; it means exactly what it always did —
    agent-writable, therefore contained.
    """
    return root_trust(root_name) is RootTrust.UNTRUSTED


def pin_name_for_path(artifact: Path) -> str:
    """The name :class:`~arctrust.TofuLayer` keys the gated ``artifact`` on.

    A tool pins under its file stem and a skill under its FOLDER name
    (``SKILL.md``'s parent), while a skill's displayed name is its frontmatter
    name, which can differ. Deriving the pin name from the artifact path keeps
    an approval aligned with what the loader will look up — the loader gates
    through this function and ``arc trust approve`` pins through it, so the two
    cannot spell the same artifact differently.

    An artifact in the deployment module root pins under its path WITHIN that
    root (``scheduler/capabilities``, ``memory/skills/recall``). Every module
    ships a file named ``capabilities.py``, so a bare stem would give all of
    them ONE pin name: approving the second module would supersede the first's
    hash, and the loader would then read the first as drifted — tamper, a hard
    DENY — for no reason but the name.
    """
    relative = _module_relative(artifact)
    if relative is None:
        return artifact.parent.name if artifact.name == SKILL_MANIFEST else artifact.stem
    base = relative.parent if artifact.name == SKILL_MANIFEST else relative.with_suffix("")
    return base.as_posix()


def _module_relative(artifact: Path) -> PurePosixPath | None:
    """``artifact``'s path within the deployment module root, or None if outside.

    Resolved against :func:`module_root` at call time, the same way the loader's
    scan roots are built, so a relocated ``ARC_CONFIG_DIR`` moves both together.
    """
    from arcagent.core.module_discovery import module_root

    try:
        relative = artifact.resolve().relative_to(module_root().expanduser().resolve())
    except (OSError, ValueError):
        return None
    return PurePosixPath(relative.as_posix()) if len(relative.parts) > 1 else None


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
        trusted_public_keys: tuple[bytes, ...] = (),
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
        # floor (enterprise/federal); ``trusted_public_keys`` is the SET of keys
        # a signature may be pinned to — the agent's own DID key plus every
        # operator key pinned in ``[security.validators]``, because an
        # operator-signed and an agent-self-signed capability must both be able
        # to pass. All default off so a bare library loader keeps pre-SPEC-033
        # behaviour — production wires them.
        self._tofu = tofu
        self._require_signature = require_signature
        self._trusted_public_keys = trusted_public_keys
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
            trusted_public_keys=self._trusted_public_keys,
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
        """Register one ``.py`` according to its root's trust class."""
        trust = root_trust(root_name)
        if trust is RootTrust.UNTRUSTED:
            await self._register_contained_python(path, root_name, delta, seen_tools)
            return
        if trust is RootTrust.VERIFIED:
            gate = await self._passes_trust_gate(path, pin_name_for_path(path), delta)
            if not gate.allowed:
                self._record_tool_outcome(delta, path, root_name, gate.status, gate.detail)
                return
        await self._import_python(path, root_name, delta, seen_tools)

    async def _register_contained_python(
        self,
        path: Path,
        root_name: str,
        delta: _ReloadDelta,
        seen_tools: set[str],
    ) -> None:
        """Adjudicate and register agent-writable source (SPEC-033 B2/C2/D1).

        Validate imports against the tier allowlist, pass the Sign/TOFU gate,
        then represent each ``@tool`` by an ArcRun-isolated proxy — the source
        is never imported into this process.
        """
        try:
            self._ast_cache.validate(path)
        except Exception as exc:  # reason: best-effort — record + continue
            await self._record_invalid(path, root_name, delta, exc)
            return
        gate = await self._passes_trust_gate(path, pin_name_for_path(path), delta)
        if not gate.allowed:
            self._record_tool_outcome(delta, path, root_name, gate.status, gate.detail)
            return
        try:
            source = path.read_text(encoding="utf-8")
            authored_tools = parse_authored_tools(path)
        except Exception as exc:  # reason: fail closed before registration
            await self._record_invalid(path, root_name, delta, exc)
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

    async def _import_python(
        self,
        path: Path,
        root_name: str,
        delta: _ReloadDelta,
        seen_tools: set[str],
    ) -> None:
        """Import first-party source and register every decorated value in it.

        Reached by TRUSTED and VERIFIED roots alike: once the bytes are proven
        first-party, the decorators that make a module a module (``@hook``,
        ``@background_task``, ``@capability``) only exist on an imported object.
        """
        try:
            module = _load_module(path)
        except Exception as exc:  # reason: best-effort — record + continue
            await self._record_invalid(path, root_name, delta, exc)
            return

        for value in vars(module).values():
            meta = getattr(value, "_arc_capability_meta", None)
            if meta is None:
                continue
            await self._dispatch_capability(value, meta, path, root_name, delta, seen_tools)

    async def _record_invalid(
        self, path: Path, root_name: str, delta: _ReloadDelta, exc: BaseException
    ) -> None:
        """Record a ``.py`` that could not be read, validated, or imported."""
        detail = _short_error(exc)
        delta.errors.append((str(path), detail))
        self._record_tool_outcome(delta, path, root_name, "invalid", detail)
        await self._emit_registration_failed(path, "python", detail)

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
        an unpinned floor is no floor. Fail closed — an EMPTY key set is exactly
        that unpinned floor and denies.
        """
        if self._tofu is None and not self._require_signature:
            return _GateResult(allowed=True)
        if self._require_signature and not self._trusted_public_keys:
            await self._deny_capability(path, "signature", "signature required but no pinned key")
            delta.errors.append((str(path), "signature: required but no pinned key — denied"))
            return _GateResult(
                allowed=False, status="unsigned", detail="required but no pinned key"
            )
        try:
            source_bytes = path.read_bytes()
            signed = self._verify_against_any_key(path, source_bytes)
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

    def _verify_against_any_key(self, path: Path, content: bytes) -> bool:
        """True when ``content``'s sidecar verifies against ANY pinned key.

        The backend contract stays singular-key — one call per pinned key — so
        every other consumer of :class:`TrustBackend` is untouched. An empty set
        means no pin at all: only reachable when a signature is not the floor
        (the gate denies an unpinned floor before we get here), where an
        unpinned verify asks for attribution rather than authority.
        """
        if not self._trusted_public_keys:
            return self._trust_backend.verify(path, content, trusted_public_key=None)
        return any(
            self._trust_backend.verify(path, content, trusted_public_key=key)
            for key in self._trusted_public_keys
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

        Name falls back to the TOFU pin name and version/description are empty —
        the module never ran, so no ``@tool`` metadata exists. The pin name
        rather than the bare stem, because this is the name an operator types
        into ``arc trust approve``, and every module's file is ``capabilities.py``.
        """
        delta.outcomes.append(
            CapabilityOutcome(
                kind="tool",
                name=pin_name_for_path(path),
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
        # SKILL.md is injected into the agent prompt (LLM01/ASI06), so any skill
        # folder outside the wheel passes the same Sign/TOFU gate as a .py —
        # agent-writable (UNTRUSTED) and bundle-delivered (VERIFIED) alike.
        if root_trust(root_name) is not RootTrust.TRUSTED:
            gate = await self._passes_trust_gate(skill_md, pin_name_for_path(skill_md), delta)
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
    "MODULE_ROOT_PREFIX",
    "SKILL_MANIFEST",
    "CapabilityLoader",
    "CapabilityOutcome",
    "RootTrust",
    "ScanRoot",
    "is_untrusted_root",
    "pin_name_for_path",
    "root_trust",
]
