"""SPEC-062 COMP-002 — resolve an extension name to a bundle, with a trust verdict.

This is the point where an operator-supplied string becomes a filesystem path,
so it carries the two controls the adapter registry established
(``arcgateway/adapters/registry.py``) and nothing else:

* **Authorize** — names are regex-validated before they reach the filesystem, so
  ``../evil`` or ``os.system`` never resolve (ASI04 / NIST SI-10). Containment of
  the resolved path is the second line: a bundle that symlinks out of the
  extensions root is refused even though its name was well formed.
* **Sign** — :data:`OFFICIAL_EXTENSIONS` is the vetted-upstream allowlist and the
  load-time control point signature verification attaches to. An unlisted bundle
  loads at personal/enterprise with a recorded warning (self-signed posture) and
  is **refused** at federal, where the signed allowlist is the whole point.

Every verdict — allow, warn, refuse — goes through the single
:func:`arctrust.audit.emit` chokepoint. A control that decides correctly but
records nothing is not a control.

The catalog names no upstream: the shipping allowlist is populated by the
deployment, not by this module.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier

_logger = logging.getLogger("arcagent.extension.catalog")

#: Extension names: lowercase, start with a letter, max 32 chars. Same shape as
#: the adapter/provider guards — blocks "../evil", "os.system", etc.
_VALID_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: DID of the catalog itself, used when a caller supplies no agent identity.
_CATALOG_DID = "did:arc:extension-catalog"

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

    Attributes:
        name: The validated extension name.
        path: The bundle directory inside the extensions root.
        official: True when the name is on :data:`OFFICIAL_EXTENSIONS`. False
            means the bundle loaded on an audited warning and is untrusted
            upstream — the loader keeps its trust gate regardless.
    """

    name: str
    path: Path
    official: bool


class ExtensionCatalog:
    """Resolves an extension name to a bundle path plus an official/unofficial verdict.

    Args:
        root: The extensions root. Every resolved bundle must live directly inside it.
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
        root: Path,
        tier: Tier,
        audit_sink: AuditSink,
        actor_did: str = _CATALOG_DID,
    ) -> None:
        self._root = Path(root)
        self._tier = tier
        self._sink = audit_sink
        self._actor_did = actor_did

    def resolve(self, name: str) -> ExtensionResolution:
        """Resolve ``name`` to a bundle, refusing anything untrusted or out of bounds.

        Args:
            name: The extension name to resolve.

        Returns:
            The bundle path and its official/unofficial verdict.

        Raises:
            ExtensionError: The name is malformed, the bundle escapes the
                extensions root, no bundle exists, or the name is unlisted at
                federal tier. Each refusal is audited before it is raised.
        """
        try:
            validate_extension_name(name)
        except ValueError as exc:
            self._refuse(name, reason="invalid_name", message=str(exc))

        path = self._root / name
        if self._root.resolve() not in path.resolve().parents:
            self._refuse(name, reason="escapes_root", message=f"{name!r} escapes the root")
        if not path.is_dir():
            self._refuse(name, reason="not_found", message=f"no bundle for {name!r} in the root")

        official = name in OFFICIAL_EXTENSIONS
        if not official:
            self._judge_unlisted(name)
        return ExtensionResolution(name=name, path=path, official=official)

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


__all__ = [
    "OFFICIAL_EXTENSIONS",
    "ExtensionCatalog",
    "ExtensionResolution",
    "validate_extension_name",
]
