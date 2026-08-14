"""`arc init --team <name> --blueprint <bp>` — project-local fleet scaffold.

team == root == fleet: scaffolds ``.arc/<team>/<agent>/`` (an isolated-workspace agent
minted via `arc agent create`), applies the blueprint config UNDER it, and writes the
blueprint persona to ``workspace/identity.md``. Reached with `arc tui --root .arc/<team>`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from arctrust.paths import arc_team

from arccli.commands.init import init_handler


@pytest.fixture(autouse=True)
def _isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirect HOME so DID key-minting + any registry write stays out of the real ~/.arcagent."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


def test_init_team_scaffolds_coder_with_persona(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    init_handler(["--team", "coding", "--blueprint", "coding", "--name", "coder"])

    # Fleets are GLOBAL under the deployment's team root, not cwd. Resolved
    # through the accessor so the assertion tracks the Arc home wherever the
    # suite's isolation puts it.
    agent_dir = arc_team("coding") / "coder"
    assert (agent_dir / "arcagent.toml").is_file()

    cfg = tomllib.loads((agent_dir / "arcagent.toml").read_text(encoding="utf-8"))
    assert cfg["agent"]["name"] == "coder"
    assert cfg["security"]["tier"] == "personal"
    assert cfg["identity"]["did"]  # a real DID was minted

    identity = (agent_dir / "workspace" / "identity.md").read_text(encoding="utf-8")
    assert "senior software engineer" in identity  # the coding persona landed


def test_init_team_default_agent_name_is_blueprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    init_handler(["--team", "coding", "--blueprint", "coding"])
    # No --name → agent named after the blueprint; global under the team root.
    assert (arc_team("coding") / "coding" / "arcagent.toml").is_file()


def test_init_team_unknown_blueprint_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        init_handler(["--team", "coding", "--blueprint", "does-not-exist"])
