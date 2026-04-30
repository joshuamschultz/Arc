"""Structural tests for SPEC-022 Phases 5, 6, 7 frontend modules.

Same Python-parses-JS pattern as `test_browser_bootstrap.py` and
`test_phase4_static_assets.py`. Asserts each module exposes its expected
namespace, has the contracts the agent-detail SPA depends on, and that
index.html wires the panels + script tags.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_ASSETS = _ROOT / "src" / "arcui" / "static" / "assets"
_INDEX = _ROOT / "src" / "arcui" / "static" / "index.html"


def _read(name: str) -> str:
    path = _ASSETS / name
    assert path.exists(), f"missing static asset: {name}"
    return path.read_text(encoding="utf-8")


# -------------------- Phase 5: pages --------------------


class TestAgentsPage:
    def test_module_exists(self) -> None:
        text = _read("agents-page.js")
        assert "AgentsPage" in text
        assert "mount" in text

    def test_renders_total_and_live_stats(self) -> None:
        text = _read("agents-page.js")
        # Acceptance criterion 1: both Total and Live stat boxes
        assert "Total" in text and "Live" in text


class TestAgentDetail:
    def test_module_exists(self) -> None:
        text = _read("agent-detail.js")
        assert "AgentDetail" in text
        assert "mount" in text

    @pytest.mark.parametrize("tab_id", [
        "overview", "identity", "sessions", "skills",
        "memory", "policy", "tools", "telemetry", "files",
    ])
    def test_all_nine_tabs_declared(self, tab_id: str) -> None:
        text = _read("agent-detail.js")
        # Each tab must be referenced — either as a key in TABS map or
        # as a `data-tab="<id>"` literal in the template.
        assert (
            f"'{tab_id}'" in text or f'"{tab_id}"' in text
        ), f"agent-detail.js missing tab: {tab_id}"

    def test_lazy_fetch_per_tab(self) -> None:
        text = _read("agent-detail.js")
        # Tabs declare init/dispose hooks per SDD §5.3
        assert "init" in text and "dispose" in text

    def test_routes_into_existing_endpoints(self) -> None:
        text = _read("agent-detail.js")
        # Hits the read endpoints registered in agent_detail.py. Per-agent
        # `/policy` returns bullets+raw inline; bullets are no longer
        # fetched from `/policy/bullets` separately. Same with the bullet
        # render path — `/policy/stats` is still consulted for the stat row.
        for ep in (
            "/api/agents/",
            "/config",
            "/policy",
            "/policy/stats",
            "/sessions",
            "/skills",
            "/tools",
            "/files/tree",
            "/stats",
            "/traces",
        ):
            assert ep in text, f"agent-detail.js missing endpoint: {ep}"


class TestAgentControls:
    def test_module_exists(self) -> None:
        text = _read("agent-controls.js")
        assert "AgentControls" in text

    def test_pause_and_restart_actions(self) -> None:
        text = _read("agent-controls.js")
        # SDD §D-010: Pause/Restart wired to existing /control endpoint
        assert "pause" in text.lower()
        assert "restart" in text.lower()
        assert "/control" in text

    def test_deploy_disabled_with_tooltip(self) -> None:
        text = _read("agent-controls.js")
        # Deploy button rendered, disabled, tooltip "Coming soon"
        assert "Coming soon" in text or "coming soon" in text.lower()


# -------------------- Phase 6: sidebar pages --------------------


class TestTasksPage:
    def test_module_exists(self) -> None:
        text = _read("tasks-page.js")
        assert "TasksPage" in text and "mount" in text

    def test_aggregates_across_agents(self) -> None:
        text = _read("tasks-page.js")
        # Hits fleet aggregator endpoint (acceptance criterion 11)
        assert "/api/team/tasks" in text


class TestToolsSkillsPage:
    def test_module_exists(self) -> None:
        text = _read("tools-skills-page.js")
        assert "ToolsSkillsPage" in text and "mount" in text

    def test_uses_fleet_endpoint(self) -> None:
        text = _read("tools-skills-page.js")
        # Acceptance criterion 12 — tools matrix + skills directory
        assert "/api/team/tools-skills" in text


class TestSecurityPage:
    def test_module_exists(self) -> None:
        text = _read("security-page.js")
        assert "SecurityPage" in text and "mount" in text

    def test_uses_audit_viewer_and_team_audit(self) -> None:
        text = _read("security-page.js")
        # Acceptance criterion 13 — live audit events
        assert "AuditViewer" in text
        assert "/api/team/audit" in text


class TestPolicyPage:
    def test_module_exists(self) -> None:
        text = _read("policy-page.js")
        assert "PolicyPage" in text and "mount" in text

    def test_reuses_policy_bullet_component(self) -> None:
        text = _read("policy-page.js")
        # SDD §D-006: same parser/component for detail + fleet
        assert "PolicyBullet" in text
        assert "/api/team/policy" in text


# -------------------- Phase 7: live updates --------------------


class TestLiveUpdates:
    def test_module_exists(self) -> None:
        text = _read("live-updates.js")
        assert "LiveUpdates" in text or "liveUpdates" in text

    def test_subscribe_unsubscribe(self) -> None:
        text = _read("live-updates.js")
        # SDD §4.8: subscribe:agent / unsubscribe:agent
        assert "subscribe:agent" in text
        assert "unsubscribe:agent" in text

    def test_handles_file_change_dispatch(self) -> None:
        text = _read("live-updates.js")
        # Inbound envelope is `{type: 'file_change', event_type, ...}`
        assert "file_change" in text

    def test_reconnect_resubscribes(self) -> None:
        text = _read("live-updates.js")
        # 7.4: Reconnect handling re-fires subscribe:agent for current agent
        # The reconnect hook is on the WS open event — module must dispatch
        # subscribe again from a saved current-agent-id state.
        assert "open" in text.lower() or "reconnect" in text.lower()


# -------------------- index.html wiring --------------------


_REQUIRED_PANELS = (
    "agents", "agent-detail",
    "tasks", "tools-skills", "security", "policy",
)

_REQUIRED_SCRIPTS = (
    "assets/agents-page.js",
    "assets/agent-detail.js",
    "assets/agent-controls.js",
    "assets/tasks-page.js",
    "assets/tools-skills-page.js",
    "assets/security-page.js",
    "assets/policy-page.js",
    "assets/live-updates.js",
)


class TestIndexHtmlWiring:
    @pytest.mark.parametrize("panel_id", _REQUIRED_PANELS)
    def test_panel_present(self, panel_id: str) -> None:
        text = _INDEX.read_text()
        assert (
            f'data-page-content="{panel_id}"' in text
        ), f"index.html missing panel: {panel_id}"

    @pytest.mark.parametrize("script", _REQUIRED_SCRIPTS)
    def test_script_loaded(self, script: str) -> None:
        text = _INDEX.read_text()
        assert script in text, f"index.html missing script: {script}"

    def test_init_router_called(self) -> None:
        text = _INDEX.read_text()
        # initRouter is what kicks off the route-driven app on first load
        assert "initRouter" in text or "applyRoute" in text
