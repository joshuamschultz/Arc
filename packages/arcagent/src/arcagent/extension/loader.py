"""SPEC-062 COMP-003 — the one door a third-party bundle enters the agent through.

An extension is code someone else wrote, so the only thing that must never happen is
it arriving through a door that skips the trust gate. Everything here follows from
that:

* **Every root is untrusted.** A bundle's capabilities register through
  ``extension:<name>`` roots and nothing else, so
  :func:`~arcagent.capabilities.capability_loader.is_untrusted_root` classifies them
  untrusted and :class:`CapabilityLoader` runs the AST validator + Sign gate on every
  file. A bundle outside every extensions root is refused rather than loaded from
  where it sits — a module-shaped path would otherwise inherit shipped-package trust
  (REQ-281).
* **Verified at LOAD, not at install.** Every shipped byte is re-verified against its
  ``.arcsig`` sidecar on each load, so bytes edited after a clean install are caught
  (REQ-282). No verdict is cached.
* **A required signature implies a pinned key.** Unpinned, arctrust accepts any
  self-consistent signature, so an attacker signs with their own key and passes; the
  missing pin is itself the refusal (REQ-283, mirroring ``capability_loader.py``).
* **Fail closed.** Any exception denies, and the denial is audited before it raises.

Routing follows the same split (REQ-263/264): skills and tools go to the agent through
the existing capability path; implementation code and its dependencies stay in the
extension's own folder and are never copied out; host prerequisites are handed back as
declarations for the operator to satisfy — Arc directs, it never installs (REQ-262).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.capabilities import artifact_signing
from arcagent.capabilities.capability_loader import (
    EXTENSION_ROOT_PREFIX,
    CapabilityLoader,
    ScanRoot,
)
from arcagent.capabilities.capability_registry import CapabilityRegistry, Kind
from arcagent.capabilities.inventory import append_capability_scan_roots
from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.attachment import Requirement, RequirementKind
from arcagent.extension.catalog import MANIFEST_NAME, ExtensionCatalog
from arcagent.extension.manifest import ExtensionManifest, load_manifest
from arcagent.tools._dynamic_loader import resolve_workspace_import_policy
from arcagent.tools._egress_policy import first_forbidden_egress

_logger = logging.getLogger("arcagent.extension.loader")

#: DID recorded as the actor on load verdicts when no agent identity is in hand.
_LOADER_DID = "did:arc:extension-loader"


@dataclass(frozen=True)
class LoadedExtension:
    """What a successful load produced, and what the caller must still satisfy.

    Attributes:
        name: The validated extension name.
        path: The bundle directory. Implementation code and dependencies stay
            here and are referenced from it, never copied into the agent's
            capability folder or into core (REQ-264).
        manifest: The parsed, denied-key-stripped ``extension.toml``.
        requirements: Host prerequisites and credentials the operator must supply.
            Handed back as declarations — this component installs nothing (REQ-262).
        capability_roots: The untrusted scan roots the bundle's skills and tools
            registered through (REQ-263).
    """

    name: str
    path: Path
    manifest: ExtensionManifest
    requirements: tuple[Requirement, ...]
    capability_roots: tuple[str, ...]


class ExtensionLoader:
    """Loads a bundle through the untrusted extension root, or refuses it.

    Args:
        roots: The bundle search path, in order. A loadable bundle lives directly
            inside one of them; :class:`~arcagent.extension.catalog.ExtensionCatalog`
            takes the containment decision for all of them.
        registry: The agent's capability registry the bundle's skills and tools
            register into.
        tier: Deployment tier — stringency, not a gate. Every tier verifies and
            audits; above personal an unverified bundle is refused instead of warned.
        audit_sink: Any :class:`~arctrust.audit.AuditSink`; every verdict reaches it
            through the single :func:`~arctrust.audit.emit` chokepoint.
        trusted_public_key: The operator key signatures are pinned to. ``None`` above
            personal is itself the refusal (REQ-283).
        egress_allow: ``tools.policy.egress_allow`` — the tools the operator permits
            to send data out (D-580). Consulted at enterprise; the default empty
            list is the fail-closed posture, since an operator who has permitted no
            send has permitted no send.
    """

    def __init__(
        self,
        *,
        roots: Sequence[Path],
        registry: CapabilityRegistry,
        tier: Tier,
        audit_sink: AuditSink,
        trusted_public_key: bytes | None = None,
        egress_allow: Sequence[str] = (),
    ) -> None:
        self._roots = tuple(Path(root) for root in roots)
        self._registry = registry
        self._tier = tier
        self._sink = audit_sink
        self._trusted_public_key = trusted_public_key
        self._egress_allow = tuple(egress_allow)
        self._catalog = ExtensionCatalog(
            roots=self._roots, tier=tier, audit_sink=audit_sink, actor_did=_LOADER_DID
        )

    async def load(self, name: str) -> LoadedExtension:
        """Load the named bundle from the first root on the search path holding it.

        Args:
            name: The extension name, as written by an operator.

        Returns:
            The loaded bundle, its manifest, and what the operator must still supply.

        Raises:
            ExtensionError: Any refusal at all — a malformed name, a bundle that is
                missing or escapes the root, a missing pinned key, a file that fails
                verification, an unparseable manifest, a capability the trust gate
                refused, or an unexpected error anywhere in the load. Each is audited
                before it is raised.
        """
        try:
            return await self._load(name)
        except ExtensionError:
            raise  # already audited at the point of refusal
        except Exception as exc:  # reason: fail-closed — any error denies, audited first
            self._refuse(name, reason="load_error", message=f"{type(exc).__name__}: {exc}")

    async def load_path(self, path: Path) -> LoadedExtension:
        """Load the bundle at ``path``, which must sit directly in one of the roots.

        Args:
            path: The bundle directory.

        Returns:
            The loaded bundle, exactly as :meth:`load` returns it.

        Raises:
            ExtensionError: ``path`` is outside every extensions root — loading it
                where it sits would grant it whatever trust that location carries
                (REQ-281) — or any refusal :meth:`load` raises.
        """
        bundle = Path(path)
        if all(bundle.parent.resolve() != root.resolve() for root in self._roots):
            self._refuse(
                bundle.name,
                reason="outside_extension_root",
                message=f"{bundle} is not in any extensions root {list(self._roots)}",
            )
        return await self.load(bundle.name)

    # --- Load steps, in refusal order --------------------------------------

    async def _load(self, name: str) -> LoadedExtension:
        # The catalog owns name validation and containment across every root on
        # the search path — a second copy here is a second guard to keep in step,
        # and the one that drifts is the one an attacker uses.
        bundle = self._catalog.locate(name)
        self._verify(bundle, name)
        self._catalog.resolve(name)  # official-upstream verdict, audited by the catalog
        manifest = self._read_manifest(bundle, name)
        self._refuse_forbidden_egress(manifest, name)
        roots = await self._register_capabilities(bundle, name)
        self._audit("extension.loaded", name, "allow", reason="verified")
        return LoadedExtension(
            name=name,
            path=bundle,
            manifest=manifest,
            requirements=_declared_requirements(manifest),
            capability_roots=roots,
        )

    def _verify(self, bundle: Path, name: str) -> None:
        """Re-verify every shipped byte against its sidecar, on every load.

        Independent of any install-time check and never cached: a file edited after
        a clean install fails here (REQ-282). A read that raises propagates to the
        fail-closed handler in :meth:`load`, which denies.
        """
        required = self._tier is not Tier.PERSONAL
        if required and self._trusted_public_key is None:
            self._refuse(
                name,
                reason="no_pinned_key",
                message=f"{name!r} requires a signature but no verification key is pinned",
            )
        unverified = [
            path
            for path in _signable_files(bundle)
            if not artifact_signing.verify_file(
                path, path.read_bytes(), trusted_public_key=self._trusted_public_key
            )
        ]
        if not unverified:
            return
        if required:
            self._refuse(
                name,
                reason="unsigned",
                message=f"{len(unverified)} file(s) in {name!r} are unsigned or fail verification",
            )
        self._audit("extension.unverified", name, "allow", reason="unsigned")
        _logger.warning("loader: %r loaded unverified (%d file(s))", name, len(unverified))

    def _read_manifest(self, bundle: Path, name: str) -> ExtensionManifest:
        try:
            text = (bundle / MANIFEST_NAME).read_text(encoding="utf-8")
            return load_manifest(text, tier=self._tier)
        except Exception as exc:  # reason: an unreadable/refused manifest denies the load
            self._refuse(name, reason="invalid_manifest", message=f"{type(exc).__name__}: {exc}")

    def _refuse_forbidden_egress(self, manifest: ExtensionManifest, name: str) -> None:
        """Refuse a bundle declaring a send this tier will not take from an extension.

        Ordered before capabilities register, so a refused bundle contributes
        nothing rather than being unwound afterwards. Re-run on every load and not
        only at install, because the manifest is re-read every load: a bundle that
        gains a sending verb in an upgrade, or a deployment since raised to
        federal, has to be refused before that verb exists (REQ-282's reasoning,
        applied to the egress declaration).
        """
        refusal = first_forbidden_egress(
            ((tool.name, tool.capability_tags) for tool in manifest.tools.declared),
            tier=self._tier,
            from_extension=True,
            egress_allow=self._egress_allow,
        )
        if refusal is not None:
            self._refuse(name, reason="egress_forbidden", message=refusal.message)

    async def _register_capabilities(self, bundle: Path, name: str) -> tuple[str, ...]:
        """Register the bundle's skills and tools through untrusted extension roots.

        The bundle's implementation and dependencies are not touched: only ``.py``
        files directly in the bundle and ``SKILL.md`` folders are what the capability
        path picks up, and nothing is copied out of the extension folder (REQ-264).
        """
        roots: list[ScanRoot] = []
        append_capability_scan_roots(roots, f"{EXTENSION_ROOT_PREFIX}{name}", bundle)
        loader = CapabilityLoader(
            scan_roots=roots,
            registry=self._registry,
            import_policy=resolve_workspace_import_policy(
                self._tier.value, allow_all_imports=False, allow_imports=[]
            ),
            require_signature=self._tier is not Tier.PERSONAL,
            trusted_public_key=self._trusted_public_key,
        )
        delta = await loader.scan_and_register()
        if delta.errors:
            for outcome in delta.outcomes:
                if outcome.status == "loaded":
                    await self._registry.unregister(cast("Kind", outcome.kind), outcome.name)
            self._refuse(
                name,
                reason="capability_refused",
                message="; ".join(f"{path}: {detail}" for path, detail in delta.errors),
            )
        return tuple(root for root, _ in roots)

    # --- Verdicts -----------------------------------------------------------

    def _refuse(self, name: str, *, reason: str, message: str) -> NoReturn:
        """Audit the denial, then raise."""
        self._audit("extension.blocked", name, "deny", reason=reason)
        _logger.warning("loader: refused extension %r (%s)", name, reason)
        raise ExtensionError(code="EXTENSION_REFUSED", message=message, details={"reason": reason})

    def _audit(self, action: str, name: str, outcome: str, *, reason: str) -> None:
        """Emit one verdict through the single audit chokepoint (AU-2)."""
        emit(
            AuditEvent(
                actor_did=_LOADER_DID,
                action=action,
                target=f"extension:{name}",
                outcome=outcome,
                tier=self._tier.value,
                extra={"extension": name, "reason": reason},
            ),
            self._sink,
        )


def _signable_files(bundle: Path) -> list[Path]:
    """Every byte the bundle ships, sidecars excluded — all of it must verify."""
    return sorted(
        path
        for path in bundle.rglob("*")
        if path.is_file() and path.suffix != artifact_signing.SIDECAR_SUFFIX
    )


def _declared_requirements(manifest: ExtensionManifest) -> tuple[Requirement, ...]:
    """Everything the operator must supply, in the hook contract's vocabulary."""
    host = (
        Requirement(
            kind=RequirementKind.HOST,
            name=required.name,
            minimum_version=required.minimum_version,
            instruction=required.instruction,
        )
        for required in manifest.host_requires
    )
    credentials = (
        Requirement(kind=RequirementKind.CREDENTIAL, name=secret.name, instruction=secret.prompt)
        for secret in manifest.secrets
    )
    return (*host, *credentials)


__all__ = ["ExtensionLoader", "LoadedExtension"]
