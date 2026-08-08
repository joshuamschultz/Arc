"""SPEC-062 COMP-002 — resolve an extension name to a bundle, with a trust verdict.

This is the point where an operator-supplied string becomes a filesystem path,
so it carries the two controls the adapter registry established
(``arcgateway/adapters/registry.py``) and nothing else:

* **Authorize** — names are regex-validated before they reach the filesystem, so
  ``../evil`` or ``os.system`` never resolve (ASI04 / NIST SI-10). Containment of
  the resolved path is the second line, applied to EVERY root on the search path:
  a bundle that symlinks out of the root it sits in is refused even though its
  name was well formed.
* **Sign** — :data:`OFFICIAL_EXTENSIONS` is the vetted-upstream allowlist and the
  load-time control point signature verification attaches to. An unlisted bundle
  loads at personal/enterprise with a recorded warning (self-signed posture) and
  is **refused** at federal, where the signed allowlist is the whole point.

Every verdict — allow, warn, refuse — goes through the single
:func:`arctrust.audit.emit` chokepoint. A control that decides correctly but
records nothing is not a control.

Bundles resolve from an ORDERED search path (D-584), not from one directory:
``<agent>/extensions``, then ``$ARC_EXTENSIONS_ROOT``, then
``<arc_home>/extensions``. First hit wins **by name**, so an agent-local bundle
overrides a fleet-wide one of the same name without hiding the rest of the
fleet — a deployment ships its bundles once instead of once per agent.

The catalog names no upstream: the shipping allowlist is populated by the
deployment, not by this module.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NoReturn

from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.paths import arc_home
from pydantic import ValidationError

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.manifest import ExtensionHeader, load_manifest

_logger = logging.getLogger("arcagent.extension.catalog")

#: Extension names: lowercase, start with a letter, max 32 chars. Same shape as
#: the adapter/provider guards — blocks "../evil", "os.system", etc.
_VALID_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: DID of the catalog itself, used when a caller supplies no agent identity.
_CATALOG_DID = "did:arc:extension-catalog"

#: The only document Arc parses from a bundle (COMP-001), and therefore the one
#: thing that makes a directory a bundle rather than a folder.
MANIFEST_NAME = "extension.toml"

#: The directory name bundles live under, beside an agent config and under arc home.
BUNDLES_DIRNAME = "extensions"

#: Deployment-wide override for the middle root of the search path.
EXTENSIONS_ROOT_ENV = "ARC_EXTENSIONS_ROOT"


def resolve_extension_roots(agent_dir: Path | None = None) -> tuple[Path, ...]:
    """Return the ordered bundle search path, dropping roots that do not exist (D-584).

    Order is agent-local, then deployment override, then user-wide:
    ``<agent_dir>/extensions``, ``$ARC_EXTENSIONS_ROOT``,
    ``<arc_home>/extensions``. Every surface — CLI, TUI, web — calls this rather
    than composing its own order, because two orders would mean an operator
    installing through one surface and an agent reading through another disagree
    about which directory a given bundle name refers to.

    Args:
        agent_dir: The agent directory holding ``arcagent.toml``. ``None`` asks
            the fleet-wide question, which is what a surface has before an
            operator has chosen an agent.

    Returns:
        Existing directories, in search order, without duplicates.
    """
    candidates = []
    if agent_dir is not None:
        candidates.append(Path(agent_dir).expanduser() / BUNDLES_DIRNAME)
    override = os.environ.get(EXTENSIONS_ROOT_ENV)
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(arc_home() / BUNDLES_DIRNAME)

    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate.is_dir() or candidate.resolve() in seen:
            continue
        seen.add(candidate.resolve())
        roots.append(candidate)
    return tuple(roots)


#: Vetted upstream extensions → their expected distribution package name.
#: At federal tier only these names may resolve (signed-allowlist control point);
#: the pinned version and sha256 of the artifact travel on the bundle's own
#: manifest, which is verified against this expected distribution at load time.
OFFICIAL_EXTENSIONS: dict[str, str] = {}


def validate_extension_name(name: str) -> None:
    """Reject extension names that could enable path traversal or injection.

    Args:
        name: Extension name from operator config, a CLI argument, or a manifest.

    Raises:
        ValueError: If ``name`` does not match ``[a-z][a-z0-9_]{0,31}``.
    """
    if not _VALID_NAME_RE.match(name):
        msg = f"invalid extension name {name!r}; must match [a-z][a-z0-9_]{{0,31}}"
        raise ValueError(msg)


@dataclass(frozen=True)
class ExtensionResolution:
    """Where a named extension lives, and whether it came from a vetted upstream.

    ``error`` is the discriminator a listing caller reads: empty means this IS a
    bundle and ``version``/``description`` describe it; non-empty means the
    directory looked like a bundle and its manifest would not parse (or would not
    load at this tier), and ``error`` says why. :meth:`ExtensionCatalog.resolve`
    never returns an error entry — it raises instead — so the two fields are
    populated only by :meth:`ExtensionCatalog.available`, which reads manifests.

    Attributes:
        name: The extension name. Validated, except on an error entry naming a
            directory whose name is itself what makes it unusable.
        path: The bundle directory inside the root it resolved from.
        official: True when the name is on :data:`OFFICIAL_EXTENSIONS`. False
            means the bundle loaded on an audited warning and is untrusted
            upstream — the loader keeps its trust gate regardless.
        version: The manifest's declared version. Empty unless the manifest was read.
        description: The manifest's one-line description, written for a person
            choosing from a list. Empty unless the manifest was read.
        error: Why this directory is not a usable bundle. Empty when it is one.
    """

    name: str
    path: Path
    official: bool
    version: str = ""
    description: str = ""
    error: str = ""


class ExtensionCatalog:
    """Resolves an extension name to a bundle path plus an official/unofficial verdict.

    Args:
        roots: The bundle search path, in order. A resolved bundle must live
            directly inside the root it was found in; the first root holding the
            name wins (D-584). :func:`resolve_extension_roots` composes the
            deployment's order.
        tier: Deployment tier — stringency, not a gate: every tier validates,
            authorizes, and audits; only the unlisted-bundle verdict differs.
        audit_sink: Any :class:`~arctrust.audit.AuditSink`; every verdict is written
            to it through :func:`~arctrust.audit.emit`.
        actor_did: DID recorded as the actor on each event. Defaults to the
            catalog's own component DID when no agent identity is in hand.
    """

    def __init__(
        self,
        *,
        roots: Sequence[Path],
        tier: Tier,
        audit_sink: AuditSink,
        actor_did: str = _CATALOG_DID,
    ) -> None:
        self._roots = tuple(Path(root) for root in roots)
        self._tier = tier
        self._sink = audit_sink
        self._actor_did = actor_did

    def resolve(self, name: str) -> ExtensionResolution:
        """Resolve ``name`` to a bundle, refusing anything untrusted or out of bounds.

        Args:
            name: The extension name to resolve.

        Returns:
            The bundle path and its official/unofficial verdict, from the first
            root on the search path that holds the name.

        Raises:
            ExtensionError: The name is malformed, the bundle escapes the root it
                sits in, no root holds it, or the name is unlisted at federal
                tier. Each refusal is audited before it is raised.
        """
        path = self.locate(name)
        official = name in OFFICIAL_EXTENSIONS
        if not official:
            self._judge_unlisted(name)
        return ExtensionResolution(name=name, path=path, official=official)

    def locate(self, name: str) -> Path:
        """Find ``name``'s bundle on the search path, taking no provenance verdict.

        Split out of :meth:`resolve` so the one caller that must verify signatures
        BEFORE judging provenance — :class:`~arcagent.extension.loader.
        ExtensionLoader`, where an unsigned bundle is the more actionable refusal
        — still shares this containment implementation rather than keeping a
        second copy of it.

        Args:
            name: The extension name to locate.

        Returns:
            The bundle directory, from the first root that holds it.

        Raises:
            ExtensionError: The name is malformed, the bundle escapes the root it
                sits in, or no root holds it. Audited before it is raised.
        """
        try:
            validate_extension_name(name)
        except ValueError as exc:
            self._refuse(name, reason="invalid_name", message=str(exc))
        for root in self._roots:
            path = root / name
            if not path.is_dir():
                continue
            if root.resolve() not in path.resolve().parents:
                self._refuse(
                    name, reason="escapes_root", message=f"{name!r} escapes the root {root}"
                )
            return path
        self._refuse(
            name, reason="not_found", message=f"no bundle for {name!r} on the search path"
        )

    def available(self) -> tuple[ExtensionResolution, ...]:
        """List every bundle the search path holds, earlier roots winning by name.

        This is what a picker reads, so it never raises on a bundle it cannot
        read: an unparseable manifest — or one this tier refuses — comes back as
        an entry whose ``error`` says why (see :class:`ExtensionResolution`). One
        broken directory blanking the whole catalog would be a far worse failure
        than a listed bundle an operator cannot install.

        Nothing is audited here for the same reason ``arc connector list`` audits
        nothing: no verdict is taken. The refusal an operator acts on is taken by
        :meth:`resolve`, and that one is recorded.

        Returns:
            One entry per name, in search order then alphabetical within a root.
        """
        entries: list[ExtensionResolution] = []
        seen: set[str] = set()
        for root in self._roots:
            for path in _bundle_dirs(root):
                if path.name in seen:
                    continue
                seen.add(path.name)
                entries.append(self._describe(root, path))
        return tuple(entries)

    # --- internals ----------------------------------------------------------

    def _describe(self, root: Path, path: Path) -> ExtensionResolution:
        """Read one candidate directory into a listing entry, never raising.

        The manifest is parsed by the shipped parser at the deployment's tier, so
        a bundle this tier would refuse is listed with that refusal as its reason
        rather than advertised as installable.
        """
        entry = ExtensionResolution(
            name=path.name, path=path, official=path.name in OFFICIAL_EXTENSIONS
        )
        if root.resolve() not in path.resolve().parents:
            return replace(entry, error=f"{path.name!r} escapes the root {root}")
        try:
            validate_extension_name(path.name)
            header = _read_header(path, self._tier)
        except (OSError, ValueError, ValidationError, ExtensionError) as exc:
            return replace(entry, error=f"{type(exc).__name__}: {exc}")
        return replace(entry, version=header.version, description=header.description)

    def _judge_unlisted(self, name: str) -> None:
        """Refuse an unlisted bundle at federal; allow it with a recorded warning below."""
        if self._tier == Tier.FEDERAL:
            self._refuse(
                name,
                reason="not_official",
                message=f"extension {name!r} is not in the federal allowlist",
            )
        self._audit("extension.unverified", name, "allow", reason="not_official")
        _logger.warning(
            "catalog: resolving unvetted extension %r (personal/enterprise only)", name
        )

    def _refuse(self, name: str, *, reason: str, message: str) -> NoReturn:
        """Audit the denial, then raise."""
        self._audit("extension.blocked", name, "deny", reason=reason)
        _logger.warning("catalog: refused extension %r (%s)", name, reason)
        raise ExtensionError(code="EXTENSION_REFUSED", message=message, details={"reason": reason})

    def _audit(self, action: str, name: str, outcome: str, *, reason: str) -> None:
        """Emit one verdict through the single audit chokepoint (AU-2)."""
        emit(
            AuditEvent(
                actor_did=self._actor_did,
                action=action,
                target=f"extension:{name}",
                outcome=outcome,
                tier=self._tier.value,
                extra={"extension": name, "reason": reason},
            ),
            self._sink,
        )


def _bundle_dirs(root: Path) -> list[Path]:
    """Every directory in ``root`` holding a manifest, alphabetically.

    A root that does not exist yields nothing: a machine without a fleet bundle
    directory has no bundles, which is ordinary and not an error.
    """
    if not root.is_dir():
        return []
    return sorted(path.parent for path in root.glob(f"*/{MANIFEST_NAME}") if path.is_file())


def _read_header(bundle: Path, tier: Tier) -> ExtensionHeader:
    """The ``[extension]`` table of one bundle, through the shipped parser."""
    text = (bundle / MANIFEST_NAME).read_text(encoding="utf-8")
    return load_manifest(text, tier=tier).extension


__all__ = [
    "BUNDLES_DIRNAME",
    "EXTENSIONS_ROOT_ENV",
    "MANIFEST_NAME",
    "OFFICIAL_EXTENSIONS",
    "ExtensionCatalog",
    "ExtensionResolution",
    "resolve_extension_roots",
    "validate_extension_name",
]
