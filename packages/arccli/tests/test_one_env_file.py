"""The deployment has exactly ONE environment file, and every path agrees on it.

There were two. ``arc init`` created ``~/.arc/.env``; the systemd unit sourced
``~/.arc/arc.env``; ``arc gateway connect-telegram`` wrote to ``arc.env``. A
provider key stored through the key store landed in ``.env``, which nothing in a
running deployment read — so the store reported ``present`` forever while the
provider stayed unreachable. That is the worst failure a write-only store can
have: it cannot be seen by reading the store back, because reading it back is
exactly what the store refuses to do.

These tests pin the agreement rather than the string, so the name can move as
long as everything moves together.
"""

from __future__ import annotations

from pathlib import Path

from arcagent.keys import default_env_file

from arccli.commands.agent._common import _ENV_PATHS as _AGENT_ENV_PATHS
from arccli.commands.run import _ENV_PATHS as _RUN_ENV_PATHS


def test_the_key_store_writes_the_file_the_deployment_sources() -> None:
    """The systemd unit's ``EnvironmentFile=%h/.arc/arc.env`` is the contract."""
    assert default_env_file(Path("/tmp/arcworld")) == Path("/tmp/arcworld/arc.env")


def test_every_env_loader_reads_the_file_the_key_store_writes() -> None:
    """A key set by any surface must be loadable by ``arc run`` and ``arc agent``.

    Both loaders search a list; what matters is that the user-wide entry is the
    same file :func:`default_env_file` writes — not that the list is identical.
    """
    written = default_env_file(Path.home() / ".arc")
    for name, paths in (("run", _RUN_ENV_PATHS), ("agent", _AGENT_ENV_PATHS)):
        assert written in paths, f"the {name} env loader never reads {written}"


def test_no_surface_still_points_at_the_abandoned_dot_env() -> None:
    """``~/.arc/.env`` is gone. A loader still listing it would resurrect the split."""
    abandoned = Path.home() / ".arc" / ".env"
    assert abandoned not in _RUN_ENV_PATHS
    assert abandoned not in _AGENT_ENV_PATHS
