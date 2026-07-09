"""SPEC-053 T-11 — `arc init` generates the deployment operator key.

The operator key is the audit authority for every WORM chain. `arc init`
creates it operator-side, once, at a well-known path with `0600`/`0700`
permissions and zero prompts at personal tier. arccli delegates all crypto to
arctrust (no key logic here).
"""

from __future__ import annotations

import argparse
import stat
from pathlib import Path

from arctrust.operator import OperatorKey

from arccli.commands.init import _init


def _run_init(tier: str, config_dir: Path, *, quick: bool = False) -> None:
    _init(
        argparse.Namespace(
            tier=tier, config_dir=str(config_dir), provider="anthropic", quick=quick
        )
    )


def test_init_creates_operator_key_with_secure_modes(tmp_path: Path) -> None:
    _run_init("personal", tmp_path)
    key = tmp_path / "operator" / "operator.key"
    assert key.exists()
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700
    # A real, loadable Ed25519 operator key.
    op = OperatorKey.load(key)
    assert len(op.seed) == 32


def test_init_is_idempotent_on_operator_key(tmp_path: Path) -> None:
    _run_init("personal", tmp_path, quick=True)
    first = (tmp_path / "operator" / "operator.key").read_bytes()
    _run_init("personal", tmp_path, quick=True)  # re-run must NOT regenerate the key
    second = (tmp_path / "operator" / "operator.key").read_bytes()
    assert first == second


def test_init_prints_operator_fingerprint_at_federal(
    tmp_path: Path, capsys: object
) -> None:
    _run_init("federal", tmp_path)
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    key = tmp_path / "operator" / "operator.key"
    assert key.exists()
    # Enterprise/federal print the operator pubkey fingerprint for out-of-band
    # recording (anti-genesis-substitution + witness bootstrap).
    op = OperatorKey.load(key)
    assert op.public_key.hex()[:16] in out
