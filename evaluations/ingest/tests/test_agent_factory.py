"""Workspace containment tests — COMP-006 / REQ-198.

`ArcAgent.__init__` resolves a relative `[agent] workspace` against
`config_path.parent` only when a `config_path` is passed; otherwise it resolves
against the process CWD, which for this harness is the Arc repo root. The
workspace, the trace store, the audit chain and the capability scan root all
hang off that one decision, so a factory that forgets `config_path` writes an
agent's brain into the repository. The guard under test is what makes that
failure loud instead of silent.

Containment is pure path logic and is tested as such: no `startup()`, no
provider, no network, nothing written outside `tmp_path`. Two tests do build a
real `ArcAgent` — construction only — to prove the guard's verdict against the
actual resolution rule rather than a restatement of it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from evaluations.ingest.agent_factory import (
    WorkspaceEscapeError,
    assert_workspace_contained,
)


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """A per-question run directory, pre-resolved.

    macOS hands pytest a `/private/var` tree reachable as `/var`; resolving the
    fixture keeps the tests honest about what the guard must normalize instead
    of accidentally passing because both sides happened to match.
    """
    path = (tmp_path / "runs" / "lme-q0001").resolve()
    path.mkdir(parents=True)
    return path


@pytest.fixture
def isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME and the Arc config root at throwaway dirs.

    Two reasons. The developer's `~/.arc/config/arcagent.toml` merges under every
    per-agent config (`_compose_raw_config`), so an unisolated run would read a
    workspace value the harness never wrote. And COMP-006 exists precisely to
    avoid `arc agent create`, which mints identities into `~/.arcagent/keys` —
    a claim only checkable against a home directory the test owns.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-config"))
    return home


def _write_agent_config(agent_dir: Path, name: str) -> Path:
    """Render the real per-agent TOML the factory emits and return its path."""
    from arccli.commands.agent._common import render_agent_config

    agent_dir.mkdir(parents=True, exist_ok=True)
    config_path = agent_dir / "arcagent.toml"
    config_path.write_text(render_agent_config(name=name, tier="personal"), encoding="utf-8")
    return config_path


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


def test_workspace_under_run_dir_is_contained(run_dir: Path) -> None:
    workspace = run_dir / "agent" / "workspace"

    assert assert_workspace_contained(workspace=workspace, run_dir=run_dir) == workspace


def test_guard_returns_the_resolved_workspace(run_dir: Path) -> None:
    """The return value is the normalized path, so callers reuse it directly."""
    workspace = run_dir / "agent" / "." / "workspace"

    assert assert_workspace_contained(workspace=workspace, run_dir=run_dir) == (
        run_dir / "agent" / "workspace"
    )


def test_repo_root_workspace_is_rejected(run_dir: Path) -> None:
    """The exact hazard REQ-198 names: a workspace resolved against the CWD."""
    workspace = Path.cwd() / "workspace"

    with pytest.raises(WorkspaceEscapeError) as excinfo:
        assert_workspace_contained(workspace=workspace, run_dir=run_dir)

    assert str(workspace) in str(excinfo.value)


def test_absolute_workspace_elsewhere_is_rejected(tmp_path: Path, run_dir: Path) -> None:
    workspace = (tmp_path / "somewhere-else" / "workspace").resolve()

    with pytest.raises(WorkspaceEscapeError):
        assert_workspace_contained(workspace=workspace, run_dir=run_dir)


def test_dotdot_escape_is_rejected(run_dir: Path) -> None:
    """A path that only looks contained until it is normalized."""
    workspace = run_dir / "agent" / ".." / ".." / "escaped" / "workspace"

    with pytest.raises(WorkspaceEscapeError):
        assert_workspace_contained(workspace=workspace, run_dir=run_dir)


def test_dotdot_that_lands_back_inside_is_contained(run_dir: Path) -> None:
    """Normalization, not a textual `..` ban — this path really is contained."""
    workspace = run_dir / "agent" / ".." / "agent" / "workspace"

    assert assert_workspace_contained(workspace=workspace, run_dir=run_dir) == (
        run_dir / "agent" / "workspace"
    )


def test_symlinked_workspace_escaping_run_dir_is_rejected(tmp_path: Path, run_dir: Path) -> None:
    """`run_dir/agent/workspace` exists and still points out of the run dir."""
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    agent_dir = run_dir / "agent"
    agent_dir.mkdir()
    (agent_dir / "workspace").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceEscapeError):
        assert_workspace_contained(workspace=agent_dir / "workspace", run_dir=run_dir)


def test_symlinked_run_dir_still_contains_its_real_workspace(tmp_path: Path) -> None:
    """Both sides are normalized, so a symlinked run dir is not a false escape."""
    real = (tmp_path / "real-run").resolve()
    (real / "agent").mkdir(parents=True)
    link = tmp_path / "linked-run"
    link.symlink_to(real, target_is_directory=True)

    result = assert_workspace_contained(workspace=link / "agent" / "workspace", run_dir=link)

    assert result == real / "agent" / "workspace"


def test_sibling_prefix_is_not_containment(tmp_path: Path) -> None:
    """`/x/run-evil` shares a string prefix with `/x/run` and is still outside."""
    base = tmp_path.resolve()
    run = base / "run"
    run.mkdir()
    workspace = base / "run-evil" / "workspace"

    with pytest.raises(WorkspaceEscapeError):
        assert_workspace_contained(workspace=workspace, run_dir=run)


def test_containment_does_not_require_the_paths_to_exist(tmp_path: Path) -> None:
    """The guard runs before scaffold, so it may not create or stat anything."""
    run = (tmp_path / "not-created-yet").resolve()
    workspace = run / "agent" / "workspace"

    assert assert_workspace_contained(workspace=workspace, run_dir=run) == workspace
    assert not run.exists()


# ---------------------------------------------------------------------------
# The guard against the real ArcAgent resolution rule
# ---------------------------------------------------------------------------


def test_guard_accepts_an_agent_built_with_an_absolute_config_path(
    run_dir: Path, isolated_arc_home: Path
) -> None:
    """render_agent_config -> load_config -> ArcAgent(config_path=<abs>) passes.

    Construction only. `startup()` is never called: it would mint identity,
    open the trace store and start module loops, none of which containment
    depends on.
    """
    from arcagent.core.agent import ArcAgent
    from arcagent.core.config import load_config

    config_path = _write_agent_config(run_dir / "agent", "lme-q0001")
    agent = ArcAgent(load_config(config_path), config_path=config_path)

    assert assert_workspace_contained(workspace=agent._workspace, run_dir=run_dir) == (
        run_dir / "agent" / "workspace"
    )


def test_guard_rejects_an_agent_built_without_a_config_path(
    run_dir: Path, isolated_arc_home: Path
) -> None:
    """Omitting `config_path` puts the workspace under the process CWD."""
    from arcagent.core.agent import ArcAgent
    from arcagent.core.config import load_config

    config_path = _write_agent_config(run_dir / "agent", "lme-q0001")
    agent = ArcAgent(load_config(config_path))

    assert agent._workspace == Path.cwd() / "workspace"
    with pytest.raises(WorkspaceEscapeError):
        assert_workspace_contained(workspace=agent._workspace, run_dir=run_dir)


def test_building_an_agent_config_never_touches_the_home_directory(
    run_dir: Path, isolated_arc_home: Path
) -> None:
    """COMP-006 replicates the scaffold rather than calling `arc agent create`.

    `arc agent create` mints keypairs into `~/.arcagent/keys` and registers the
    agent; a throwaway per-question agent must leave no trace off the run dir.
    """
    from arcagent.core.agent import ArcAgent
    from arcagent.core.config import load_config

    config_path = _write_agent_config(run_dir / "agent", "lme-q0001")
    agent = ArcAgent(load_config(config_path), config_path=config_path)
    assert_workspace_contained(workspace=agent._workspace, run_dir=run_dir)

    assert list(isolated_arc_home.iterdir()) == []
    assert os.environ["HOME"] == str(isolated_arc_home)
