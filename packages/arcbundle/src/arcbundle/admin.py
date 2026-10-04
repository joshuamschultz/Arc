"""What an operator surface needs around a bundle, shared by every surface.

``arc module`` and the dashboard's Maintenance tab both have to list the staged
bundles, decide which tier a bundle is verified at, decide which issuer keys are
trusted for it, and put a verified bundle on disk for one agent. Written twice,
the second copy would one day trust something the first refuses; written here,
there is one answer.

Nothing in this module trusts what it reads. :func:`staged_bundles` and
:func:`peek_issuer` read *claims* out of an unverified manifest, only to label a
row or to look up the key that must then verify the signature; the verdict always
comes from :func:`arcbundle.verify_bundle`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from arctrust.trust_store import TrustStoreError, load_issuer_pubkey

from arcbundle.capability_copy import copy_capabilities
from arcbundle.errors import BundleError, BundleTierError
from arcbundle.manifest import BundleManifest
from arcbundle.materializer import materialize
from arcbundle.verifier import MANIFEST_NAME, TIERS, VerifiedBundle

_logger = logging.getLogger("arcbundle.admin")

#: A staged bundle is a directory named ``<module>.arcbundle``.
BUNDLE_SUFFIX = ".arcbundle"

#: Increasing stringency. Used to take the stricter of two tiers, never to widen one.
_TIER_ORDER = ("personal", "enterprise", "federal")

#: Trust-store answers that mean "no policy entry", not "the store is broken".
_ABSENT_ENTRY = ("TRUST_STORE_DID_UNKNOWN", "TRUST_STORE_FILE_MISSING")

__all__ = [
    "BUNDLE_SUFFIX",
    "StagedBundle",
    "bundle_matches_installed",
    "install_verified",
    "peek_issuer",
    "staged_bundles",
    "trusted_issuers",
    "verification_tier",
]


@dataclass(frozen=True)
class StagedBundle:
    """One bundle waiting in the staging directory, as its manifest *claims*.

    ``version`` and ``issuer`` are ``None`` when the manifest cannot be read; the
    row is still listed so an operator can see that something is there, and
    verification refuses it later.
    """

    module: str
    path: Path
    version: str | None
    issuer: str | None


def _claimed(bundle_root: Path) -> tuple[str | None, str | None]:
    """The ``(version, issuer)`` an unverified manifest claims, or ``(None, None)``."""
    try:
        data = json.loads((bundle_root / MANIFEST_NAME).read_bytes())
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    version, issuer = data.get("version"), data.get("issuer")
    return (
        version if isinstance(version, str) else None,
        issuer if isinstance(issuer, str) else None,
    )


def staged_bundles(store: Path) -> dict[str, StagedBundle]:
    """Module name to staged bundle, for everything in ``store``."""
    if not store.is_dir():
        return {}
    found: dict[str, StagedBundle] = {}
    for path in sorted(store.iterdir()):
        if not (path.is_dir() and path.name.endswith(BUNDLE_SUFFIX)):
            continue
        module = path.name[: -len(BUNDLE_SUFFIX)]
        version, issuer = _claimed(path)
        found[module] = StagedBundle(module=module, path=path, version=version, issuer=issuer)
    return found


def bundle_matches_installed(bundle: StagedBundle, modules_root: Path) -> bool:
    """True when the installed module holds exactly the files this bundle declares.

    A content comparison, not a version comparison: the installed tree carries no
    version stamp, and two builds can share a version number. Unreadable or
    missing on either side is "not the same", which is the safe answer for a page
    that offers an update.
    """
    try:
        manifest = BundleManifest.model_validate_json((bundle.path / MANIFEST_NAME).read_bytes())
    except (OSError, ValueError, BundleError):
        return False
    installed = modules_root / manifest.module
    if installed.is_symlink() or not installed.is_dir():
        return False
    for entry in manifest.files:
        try:
            actual = hashlib.sha256((installed / entry.path).read_bytes()).hexdigest()
        except OSError:
            return False
        if actual != entry.sha256:
            return False
    return True


def peek_issuer(bundle_root: Path) -> str:
    """Read the issuer a bundle *claims*, only in order to look up a key for it.

    A name is not a permission. An unknown name resolves to no key and the bundle
    is refused; a known name still has to produce that issuer's signature.
    """
    return _claimed(bundle_root)[1] or ""


def _configured_tier(config_path: Path) -> str | None:
    """``[security].tier`` from a TOML file, or ``None`` when it says nothing."""
    if not config_path.is_file():
        return None
    try:
        block = tomllib.loads(config_path.read_text(encoding="utf-8")).get("security", {})
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise BundleTierError(f"cannot read the security tier from {config_path}: {exc}") from exc
    tier = block.get("tier") if isinstance(block, dict) else None
    if tier is None:
        return None
    if tier not in TIERS:
        raise BundleTierError(f"{config_path} declares an unknown tier {tier!r}")
    return str(tier)


def verification_tier(*config_paths: Path | None) -> str:
    """The strictest tier any of ``config_paths`` declares; ``personal`` when none do.

    Callers pass the machine config and the target agent's config. Taking the
    maximum means installing for a federal agent applies federal rules even when
    the machine config has not caught up. Raises :class:`BundleTierError` for a
    config that cannot be read or names a tier this build does not know.
    """
    declared = [
        tier
        for path in config_paths
        if path is not None and (tier := _configured_tier(path)) is not None
    ]
    if not declared:
        return "personal"
    return max(declared, key=_TIER_ORDER.index)


def trusted_issuers(
    bundle_root: Path,
    *,
    operator: tuple[str, bytes] | None,
    on_fault: Callable[[str, Exception], None] | None = None,
) -> dict[str, bytes]:
    """The issuer keys this deployment accepts for ``bundle_root``.

    Two sources, both explicit: the on-box operator identity the caller resolved,
    and the claimed issuer's entry in the trust store. A trust-store failure
    leaves the issuer out rather than raising: absence is refusal, the fail-closed
    direction. "No trust file" and "no entry" are policy, not faults, and stay
    quiet; anything else goes to ``on_fault`` (logged when none is given) so a
    permissions or syntax problem is not mistaken for a policy decision.
    """
    trusted: dict[str, bytes] = {}
    if operator is not None:
        trusted[operator[0]] = operator[1]
    claimed = peek_issuer(bundle_root)
    if claimed and claimed not in trusted:
        try:
            trusted[claimed] = load_issuer_pubkey(claimed)
        except TrustStoreError as exc:
            if exc.code not in _ABSENT_ENTRY:
                (on_fault or _log_fault)(claimed, exc)
    return trusted


def _log_fault(issuer: str, exc: Exception) -> None:
    _logger.warning("trust store unusable for issuer %r: %s", issuer, exc)


def install_verified(
    verified: VerifiedBundle,
    *,
    modules_root: Path,
    agent_root: Path,
    sink: object | None = None,
    actor_did: str | None = None,
) -> Path:
    """Put one already-verified bundle on disk for one agent; return the module path.

    Materializes the module read-only at the deployment root, then writes the
    agent's capability copy. Trusting the capability signatures and enabling the
    module are the caller's next two steps, because both live in the agent
    package, which this leaf must not import.
    """
    installed = materialize(verified, modules_root, sink=sink, actor_did=actor_did)
    copy_capabilities(installed, agent_root, module=verified.manifest.module)
    return installed
