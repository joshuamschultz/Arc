"""Tests for arccli.agent_worker — federal-tier subprocess worker.

Task 26: arc-agent-worker accepts ``--did`` but historically never used it to
select which agent config to load — it always read from three fixed search
paths regardless of which agent_did a platform message was addressed to,
so a multi-agent_did gateway would silently serve the WRONG agent identity.

Fix: an optional ``--team-root`` resolves the config via a DID-indexed scan
(mirrors ``arcgateway.bootstrap._load_did_index``/``_resolve_agent_dir`` —
duplicated here rather than imported, since arccli does not depend on
arcgateway: the gateway spawns this worker as a subprocess, never the other
way around). Independent of which resolution path finds a config, the
loaded config's own ``[identity].did`` is verified against the requested
``--did`` before any agent code runs; a mismatch fails closed (refuses to
run, emits a structured audit log line) rather than silently serving the
wrong agent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arccli.agent_worker import (
    _load_did_index,
    _resolve_config_path,
    _run_with_arcagent,
)

_CONFIG_TEMPLATE = """\
[agent]
name = "{name}"
org = "local"
type = "executor"
workspace = "./workspace"

[llm]
model = "anthropic/claude-sonnet-4-5-20250929"

[identity]
did = "{did}"
key_dir = "~/.arcagent/keys"

[vault]
backend = ""

[tools.policy]
allow = []
deny = []
timeout_seconds = 30

[telemetry]
enabled = true
service_name = "{name}"
log_level = "INFO"
export_traces = false
"""


def _write_agent(team_root: Path, name: str, did: str) -> Path:
    agent_dir = team_root / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(_CONFIG_TEMPLATE.format(name=name, did=did))
    return agent_dir


# ---------------------------------------------------------------------------
# _load_did_index
# ---------------------------------------------------------------------------


class TestLoadDidIndex:
    def test_maps_did_to_agent_dir(self, tmp_path: Path) -> None:
        agent_a = _write_agent(tmp_path, "coder", "did:arc:agent:coder")
        agent_b = _write_agent(tmp_path, "trader", "did:arc:agent:trader")

        index = _load_did_index(tmp_path)

        assert index["did:arc:agent:coder"] == agent_a
        assert index["did:arc:agent:trader"] == agent_b

    def test_missing_team_root_returns_empty(self, tmp_path: Path) -> None:
        assert _load_did_index(tmp_path / "does-not-exist") == {}

    def test_agent_dir_without_did_is_skipped(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "no-did", "")
        assert _load_did_index(tmp_path) == {}

    def test_malformed_toml_is_skipped_not_raised(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "broken"
        agent_dir.mkdir()
        (agent_dir / "arcagent.toml").write_text("this is not [ valid toml")
        assert _load_did_index(tmp_path) == {}


# ---------------------------------------------------------------------------
# _resolve_config_path
# ---------------------------------------------------------------------------


class TestResolveConfigPath:
    def test_with_team_root_finds_matching_agent(self, tmp_path: Path) -> None:
        agent_dir = _write_agent(tmp_path, "coder", "did:arc:agent:coder")

        resolved = _resolve_config_path("did:arc:agent:coder", tmp_path)

        assert resolved == agent_dir / "arcagent.toml"

    def test_with_team_root_no_match_returns_none(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "coder", "did:arc:agent:coder")

        resolved = _resolve_config_path("did:arc:agent:ghost", tmp_path)

        assert resolved is None

    def test_without_team_root_falls_back_to_search_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixed_config = tmp_path / "arcagent.toml"
        fixed_config.write_text(_CONFIG_TEMPLATE.format(name="fixed", did="did:arc:agent:fixed"))
        monkeypatch.setattr("arccli.agent_worker._CONFIG_SEARCH_PATHS", [fixed_config])

        resolved = _resolve_config_path("did:arc:agent:anything", None)

        assert resolved == fixed_config

    def test_without_team_root_no_search_path_exists_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("arccli.agent_worker._CONFIG_SEARCH_PATHS", [tmp_path / "nope.toml"])

        assert _resolve_config_path("did:arc:agent:anything", None) is None


# ---------------------------------------------------------------------------
# _run_with_arcagent — fail-closed identity verification
# ---------------------------------------------------------------------------


class TestDidMismatchFailsClosed:
    """The core task-26 regression: a resolved config whose declared DID does
    not match the requested ``--did`` must never be run as that agent."""

    @pytest.mark.asyncio
    async def test_mismatched_identity_refuses_to_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The classic bug: a fixed-path config belongs to a DIFFERENT agent
        than the one requested via --did. Must fail closed, not silently
        serve that other agent's identity/config."""
        wrong_agent_config = tmp_path / "arcagent.toml"
        wrong_agent_config.write_text(
            _CONFIG_TEMPLATE.format(name="wrong-agent", did="did:arc:agent:wrong-agent")
        )
        monkeypatch.setattr("arccli.agent_worker._CONFIG_SEARCH_PATHS", [wrong_agent_config])

        deltas = await _run_with_arcagent(
            "did:arc:agent:requested", "hello", "session-1", team_root=None
        )

        assert len(deltas) == 1
        assert deltas[0]["is_final"] is True
        assert "identity mismatch" in str(deltas[0]["content"]).lower()
        assert "did:arc:agent:requested" in str(deltas[0]["content"])

    @pytest.mark.asyncio
    async def test_matching_identity_proceeds_to_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sanity check: when the loaded DID matches, the worker proceeds
        past the identity gate (reaches ArcAgent construction) instead of
        refusing — proves the gate isn't a blanket denial."""
        from unittest.mock import AsyncMock, MagicMock, patch

        matching_config = tmp_path / "arcagent.toml"
        matching_config.write_text(
            _CONFIG_TEMPLATE.format(name="right-agent", did="did:arc:agent:requested")
        )
        monkeypatch.setattr("arccli.agent_worker._CONFIG_SEARCH_PATHS", [matching_config])

        mock_agent = MagicMock()
        mock_agent.startup = AsyncMock()
        mock_session = MagicMock()
        mock_agent.session = AsyncMock(return_value=mock_session)

        mock_result = MagicMock()
        mock_result.content = "real agent reply"

        with (
            patch("arcagent.core.agent.ArcAgent", return_value=mock_agent),
            patch("arccli.agent_worker.collect", AsyncMock(return_value=mock_result)),
        ):
            deltas = await _run_with_arcagent(
                "did:arc:agent:requested", "hello", "session-1", team_root=None
            )

        mock_agent.startup.assert_awaited_once()
        contents = [str(d["content"]) for d in deltas]
        assert any("real agent reply" in c for c in contents)
