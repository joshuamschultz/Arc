"""Operator-key custody for the CLI (SPEC-053).

The operator key is the deployment audit authority — it signs every WORM audit
chain and is deliberately distinct from any agent DID (the audited subject must
not hold the audit authority). ``arc init`` generates it once; direct ``arc
run`` audit uses it to sign its chain. All crypto is delegated to
``arctrust.OperatorKey`` — arccli holds no key logic of its own.
"""

from __future__ import annotations

from pathlib import Path

from arctrust import OperatorKey

# Well-known operator-key location (outside any agent workspace tool-sandbox).
DEFAULT_OPERATOR_DIR = Path("~/.arc/operator").expanduser()
_KEY_NAME = "operator.key"


def operator_key_path(arc_dir: Path) -> Path:
    """Resolve the operator-key file under an Arc config dir."""
    return Path(arc_dir).expanduser() / "operator" / _KEY_NAME


def load_operator_key(arc_dir: Path | None = None) -> OperatorKey:
    """Load the operator key, auto-bootstrapping one if absent (zero-config)."""
    base = Path(arc_dir).expanduser() if arc_dir is not None else DEFAULT_OPERATOR_DIR.parent
    return OperatorKey.load(operator_key_path(base), generate_if_absent=True)


def ensure_operator_key(arc_dir: Path) -> OperatorKey:
    """Generate + persist the operator key under ``arc_dir`` if it does not exist.

    Idempotent: an existing key is loaded and returned unchanged (never
    regenerated — regenerating would orphan every chain it has signed). Bootstrap
    is atomic and symlink/TOCTOU-safe, and a missing key with a recorded pubkey
    fails closed rather than minting a fresh one — all enforced by
    ``OperatorKey.load`` (SPEC-053 custody hardening).
    """
    return OperatorKey.load(operator_key_path(arc_dir), generate_if_absent=True)


__all__ = [
    "DEFAULT_OPERATOR_DIR",
    "ensure_operator_key",
    "load_operator_key",
    "operator_key_path",
]
