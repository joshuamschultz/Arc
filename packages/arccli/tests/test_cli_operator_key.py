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

import pytest
from arctrust.operator import OperatorKey
from arctrust.paths import default_operator_key_path

from arccli.commands.init import _init


def _run_init(tier: str, config_dir: Path, *, quick: bool = False) -> None:
    _init(
        argparse.Namespace(
            tier=tier, config_dir=str(config_dir), provider="anthropic", quick=quick
        )
    )


def test_init_creates_operator_key_with_secure_modes(tmp_path: Path) -> None:
    _run_init("personal", tmp_path)
    key = default_operator_key_path(tmp_path)
    assert key.exists()
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700
    # A real, loadable Ed25519 operator key.
    op = OperatorKey.load(key)
    assert len(op.seed) == 32


def test_init_is_idempotent_on_operator_key(tmp_path: Path) -> None:
    _run_init("personal", tmp_path, quick=True)
    first = default_operator_key_path(tmp_path).read_bytes()
    _run_init("personal", tmp_path, quick=True)  # re-run must NOT regenerate the key
    second = default_operator_key_path(tmp_path).read_bytes()
    assert first == second


def test_init_prints_operator_fingerprint_at_federal(tmp_path: Path, capsys: object) -> None:
    _run_init("federal", tmp_path)
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    key = default_operator_key_path(tmp_path)
    assert key.exists()
    # Enterprise/federal print the operator pubkey fingerprint for out-of-band
    # recording (anti-genesis-substitution + witness bootstrap).
    op = OperatorKey.load(key)
    assert op.public_key.hex()[:16] in out


def test_no_base_resolves_the_key_where_the_accessor_says(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``None`` means this deployment, which the accessor already answers.

    Substituting ``arc_home()`` for that ``None`` pinned the lookup to the
    install home. The two were the same directory until state moved beside the
    fleet; after that the pinned path held no key, and because the loader
    bootstraps one when it finds none, a SECOND operator key was minted there —
    and it signed module bundles with an issuer the trust store does not pin.
    """
    from arctrust.paths import default_operator_key_path

    from arccli.commands.operator import operator_key_path

    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "operator-root"))
    monkeypatch.delenv("ARC_CONFIG_DIR", raising=False)

    assert operator_key_path() == default_operator_key_path()
    assert operator_key_path().is_relative_to(tmp_path / "operator-root")
