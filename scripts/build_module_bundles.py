"""Package every module in the source catalog into a signed release bundle.

SPEC-066 T-970 (REQ-336). The `arc-agent` wheel carries no module code, so the
release that publishes the wheel has to publish the modules too — otherwise the
only way to get a module is to build one, and the signed-distribution path
nobody can use is the signed-distribution path nobody uses.

This runs in release CI with the Arc release key. It is deliberately NOT `arc
module bundle`: that command signs with the *deployment operator* key, which is
the right issuer for a bundle an operator built for their own box and the wrong
one for an artifact strangers will install. Both call the same
``arcbundle.build_bundle``, so the difference is the key and nothing else.

A bundle is a directory. Release assets are files, so each bundle is archived to
``<name>.arcbundle.tar.gz``; an operator expands it into
``${ARC_CONFIG_DIR:-~/.arc}/bundles/`` and installs with ``arc module install
<name>``, or points ``--from`` straight at the expanded directory.

Usage::

    python scripts/build_module_bundles.py --out dist/bundles

The signing key is read from ``ARC_RELEASE_SIGNING_KEY`` (base64 Ed25519 seed)
and the issuer from ``ARC_RELEASE_ISSUER_DID``. Both are required: an unsigned
bundle would not verify anywhere, so producing one is never the helpful
fallback it looks like.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import tarfile
from pathlib import Path

import arcagent
import arcbundle

_KEY_ENV = "ARC_RELEASE_SIGNING_KEY"
_ISSUER_ENV = "ARC_RELEASE_ISSUER_DID"

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CATALOG = _REPO_ROOT / "packages" / "arcagent" / "src" / "arcagent" / "modules"


def _signing_key() -> bytes:
    """Return the 32-byte release seed, or exit explaining what is missing."""
    raw = os.environ.get(_KEY_ENV)
    if not raw:
        sys.stderr.write(f"{_KEY_ENV} is not set; refusing to build unsigned bundles\n")
        raise SystemExit(1)
    try:
        seed = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        sys.stderr.write(f"{_KEY_ENV} is not valid base64: {exc}\n")
        raise SystemExit(1) from exc
    if len(seed) != 32:
        sys.stderr.write(f"{_KEY_ENV} decodes to {len(seed)} bytes; an Ed25519 seed is 32\n")
        raise SystemExit(1)
    return seed


def _issuer() -> str:
    issuer = os.environ.get(_ISSUER_ENV)
    if not issuer:
        sys.stderr.write(f"{_ISSUER_ENV} is not set; a bundle must name its issuer\n")
        raise SystemExit(1)
    if issuer == arcbundle.DEV_ISSUER:
        sys.stderr.write(
            f"{_ISSUER_ENV} is the development issuer {arcbundle.DEV_ISSUER!r}, which is "
            "trusted at personal tier only — it must never sign a release\n"
        )
        raise SystemExit(1)
    return issuer


def _module_names() -> list[str]:
    """Every module folder in the catalog, in a stable order."""
    if not _CATALOG.is_dir():
        sys.stderr.write(f"no module source catalog at {_CATALOG}\n")
        raise SystemExit(1)
    return sorted(
        path.name
        for path in _CATALOG.iterdir()
        if path.is_dir() and not path.name.startswith(("_", "."))
    )


def _archive(bundle: Path, out_dir: Path) -> Path:
    """Tar a bundle directory into a single publishable release asset."""
    archive = out_dir / f"{bundle.name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(bundle, arcname=bundle.name)
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="dist/bundles", help="Output directory.")
    args = parser.parse_args()

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    seed = _signing_key()
    issuer = _issuer()
    version = arcagent.__version__

    for name in _module_names():
        bundle = arcbundle.build_bundle(
            _CATALOG / name,
            module=name,
            version=version,
            private_key=seed,
            issuer=issuer,
            out=out_dir / f"{name}.arcbundle",
        )
        archive = _archive(bundle, out_dir)
        sys.stdout.write(f"built {archive.name} (module {name} {version}, issuer {issuer})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
