"""Folder-trust: persistent allowed_paths grant on `arc tui` launch."""

from __future__ import annotations

import tomllib
from pathlib import Path

from arctui.trust import folder_is_trusted, grant_folder

# The relevant slice of an `arc agent create` arcagent.toml (single-line allowed_paths
# with an inline comment — the format grant_folder must preserve).
_AGENT_TOML = """\
[agent]
name = "coder"
workspace = "./workspace"  # workspace dir (relative to this file)

[tools.policy]
allow = []
deny = []
allowed_paths = []      # filesystem paths tools may access
protected_paths = []    # read-only-to-agent paths
"""


def _write_agent(tmp_path: Path, body: str = _AGENT_TOML) -> Path:
    cfg = tmp_path / "coder" / "arcagent.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(body, encoding="utf-8")
    return cfg


def test_untrusted_folder_is_not_trusted(tmp_path: Path) -> None:
    cfg = _write_agent(tmp_path)
    project = tmp_path / "some" / "project"
    project.mkdir(parents=True)
    assert folder_is_trusted(cfg, project) is False


def test_grant_then_trusted(tmp_path: Path) -> None:
    cfg = _write_agent(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    grant_folder(cfg, project)
    assert folder_is_trusted(cfg, project) is True
    # Persisted into the toml's allowed_paths as an absolute path.
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert str(project.resolve()) in data["tools"]["policy"]["allowed_paths"]


def test_grant_preserves_inline_comment_and_other_lines(tmp_path: Path) -> None:
    cfg = _write_agent(tmp_path)
    grant_folder(cfg, tmp_path / "proj")
    text = cfg.read_text(encoding="utf-8")
    assert "# filesystem paths tools may access" in text  # inline comment survived
    assert 'protected_paths = []' in text  # neighbouring lines survived
    assert 'name = "coder"' in text


def test_grant_is_idempotent(tmp_path: Path) -> None:
    cfg = _write_agent(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    grant_folder(cfg, project)
    grant_folder(cfg, project)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["tools"]["policy"]["allowed_paths"].count(str(project.resolve())) == 1


def test_subfolder_of_trusted_is_trusted(tmp_path: Path) -> None:
    cfg = _write_agent(tmp_path)
    project = tmp_path / "proj"
    (project / "sub").mkdir(parents=True)
    grant_folder(cfg, project)
    assert folder_is_trusted(cfg, project / "sub") is True


def test_grant_when_no_allowed_paths_line(tmp_path: Path) -> None:
    cfg = _write_agent(tmp_path, '[agent]\nname = "coder"\n')
    project = tmp_path / "proj"
    project.mkdir()
    grant_folder(cfg, project)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert str(project.resolve()) in data["tools"]["policy"]["allowed_paths"]


def test_grant_folder_escapes_toml_special_chars(tmp_path: Path) -> None:
    # SEC-01: a folder name with a quote must not inject arbitrary toml into the config
    # (which the runtime trusts as security policy). The file must still parse.
    cfg = _write_agent(tmp_path)
    evil = tmp_path / 'weird"dir'
    evil.mkdir()
    grant_folder(cfg, evil)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))  # must not raise
    assert str(evil.resolve()) in data["tools"]["policy"]["allowed_paths"]
    # No stray top-level keys were injected past the intended sections.
    assert set(data) <= {"agent", "tools"}


def test_malformed_toml_reads_as_untrusted(tmp_path: Path) -> None:
    cfg = _write_agent(tmp_path, "this is not valid toml = = =\n")
    assert folder_is_trusted(cfg, tmp_path / "proj") is False
