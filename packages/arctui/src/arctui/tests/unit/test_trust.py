"""SPEC-058 T-766: folder-trust grant core (REQ-142).

Launching in a project folder grants the agent read/write there ONLY on
confirmation, as a session-scoped in-memory allowed_paths grant — never written
to arcagent.toml. The agent's fixed workspace + protected_paths stay in force.
"""

from __future__ import annotations

from pathlib import Path

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig

from arctui.trust import folder_needs_trust, grant_folder


def _config() -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(name="coder"),
        llm=LLMConfig(model="anthropic/claude-sonnet-5"),
    )


def test_untrusted_folder_needs_trust(tmp_path: Path) -> None:
    cfg = _config()
    assert folder_needs_trust(cfg, tmp_path / "proj") is True


def test_grant_adds_resolved_folder_to_allowed_paths(tmp_path: Path) -> None:
    cfg = _config()
    proj = tmp_path / "proj"
    proj.mkdir()
    grant_folder(cfg, proj)
    assert str(proj.resolve()) in cfg.tools.policy.allowed_paths


def test_granted_folder_no_longer_needs_trust(tmp_path: Path) -> None:
    cfg = _config()
    proj = tmp_path / "proj"
    proj.mkdir()
    grant_folder(cfg, proj)
    assert folder_needs_trust(cfg, proj) is False


def test_grant_is_idempotent(tmp_path: Path) -> None:
    cfg = _config()
    proj = tmp_path / "proj"
    proj.mkdir()
    grant_folder(cfg, proj)
    grant_folder(cfg, proj)
    assert cfg.tools.policy.allowed_paths.count(str(proj.resolve())) == 1


def test_grant_does_not_touch_protected_paths(tmp_path: Path) -> None:
    cfg = _config()
    before = list(cfg.tools.policy.protected_paths)
    grant_folder(cfg, tmp_path / "proj")
    assert list(cfg.tools.policy.protected_paths) == before
