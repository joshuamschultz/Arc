"""The deployment has exactly ONE environment file, and every path agrees on it.

There were two. ``arc init`` created ``~/.arc/.env``; the systemd unit sourced
``~/.arc/arc.env``; ``arc gateway connect-telegram`` wrote to ``arc.env``. A
provider key stored through the key store landed in ``.env``, which nothing in a
running deployment read — so the store reported ``present`` forever while the
provider stayed unreachable. That is the worst failure a write-only store can
have: it cannot be seen by reading the store back, because reading it back is
exactly what the store refuses to do.

These tests pin the agreement rather than the string, so the name can move as
long as everything moves together — and they now pin it under ``ARC_CONFIG_DIR``
too. The store resolved its file through ``arctrust.arc_home()`` while both
loaders hardcoded ``Path.home() / ".arc"``, so on an isolated deployment the two
sides disagreed again: the key was written where nothing read it. Same bug as
the original, invisible to a test that only ever looked at the default home.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcagent.keys import default_env_file
from arctrust import arc_home
from arctrust.paths import env_file

from arccli.commands.agent._common import _env_paths as _agent_env_paths
from arccli.commands.run import _env_paths as _run_env_paths

_LOADERS = (("run", _run_env_paths), ("agent", _agent_env_paths))

#: The shipped unit is the deployment-side half of the contract.
_UNIT = Path(__file__).resolve().parents[3] / "deploy" / "systemd" / "arc.service"


def test_the_key_store_writes_the_file_the_deployment_sources() -> None:
    """The systemd unit's ``EnvironmentFile=`` is the contract, so pin it to the unit.

    Read off the shipped unit rather than restated as a string: the store and the
    unit are the two halves that have to name one file, and a test that spells the
    path itself agrees with neither when the layout moves.
    """
    assert default_env_file(Path("/tmp/arcworld")) == env_file(Path("/tmp/arcworld"))

    sourced = next(
        line.split("=", 1)[1].strip()
        for line in _UNIT.read_text(encoding="utf-8").splitlines()
        if line.startswith("EnvironmentFile=")
    )
    assert sourced.replace("%h", "/home/arc") == str(default_env_file(Path("/home/arc/.arc")))


def test_every_env_loader_reads_the_file_the_key_store_writes() -> None:
    """A key set by any surface must be loadable by ``arc run`` and ``arc agent``.

    Both loaders search a list; what matters is that the user-wide entry is the
    same file :func:`default_env_file` writes — not that the list is identical.
    """
    written = default_env_file(arc_home())
    for name, paths in _LOADERS:
        assert written in paths(), f"the {name} env loader never reads {written}"


def test_every_env_loader_agrees_under_an_isolated_arc_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agreement must survive relocation, or the split comes back silently.

    The key store has always resolved through ``arc_home()``. When the loaders
    hardcoded the real home, ``arc keys set`` on an ``ARC_CONFIG_DIR`` deployment
    wrote a key that neither loader would ever read — reported saved, never used.
    """
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "deployment"))
    written = default_env_file()

    assert written == env_file(tmp_path / "deployment")
    for name, paths in _LOADERS:
        resolved = paths()
        assert written in resolved, f"the {name} env loader never reads {written}"
        assert Path.home() / ".arc" / "arc.env" not in resolved, (
            f"the {name} env loader still reads the invoking user's home"
        )


def test_no_surface_still_points_at_the_abandoned_dot_env() -> None:
    """``~/.arc/.env`` is gone. A loader still listing it would resurrect the split."""
    abandoned = Path.home() / ".arc" / ".env"
    for name, paths in _LOADERS:
        assert abandoned not in paths(), f"the {name} env loader resurrects {abandoned}"
