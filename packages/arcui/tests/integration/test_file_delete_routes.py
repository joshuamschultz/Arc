"""Agent workspace file editor — DELETE (H-018), hardened per security review.

Extends the existing read/write file resource with an operator-gated delete.
Drives the real Starlette app with an on-disk agent root that includes both
ordinary content and the ADR-029 protected agent-state paths: escape attempts
and viewer callers are refused exactly like the write route, blocked paths
never delete regardless of confirmation, and the confirm-required paths are
enforced at the API contract level via ``confirm_protected=true``.

BLOCKED outright (403, no override): ``identity.md``, ``policy.md`` (a
downgrade to allow-all, not a "reset"), the per-agent config TOMLs
(``arcagent.toml``/``arcllm.toml``/``arcrun.toml``), any signed prompt
overlay under ``context/**`` plus its ``.arcsig`` sidecar, any ``*.key``
private key file wherever it resolves, and the audit chain
(``workspace/audit/**``, sibling ``.audit/**``).

CONFIRM required (409 -> 200 with ``confirm_protected=true``):
``memory/**``, ``sessions/**``, ``context.md``.

Symlink/TOCTOU: ``_confine`` resolves a target's realpath exactly once, so a
workspace entry that is actually a symlink to a protected or out-of-bounds
file is judged (and audited) by what it truly points at, never by the
request string.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from arcgateway import team_roster
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware, SessionTracker
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_detail_routes
from arcui.routes.agents import routes as agent_routes


def _build_team_dir(tmp_path: Path) -> Path:
    root = tmp_path / "team"
    root.mkdir()
    agent = root / "alpha_agent"
    agent.mkdir()
    (agent / "arcagent.toml").write_text(
        '[agent]\nname = "alpha"\n[identity]\ndid = "did:arc:alpha"\n',
        encoding="utf-8",
    )
    (agent / "arcllm.toml").write_text('[llm]\nmodel = "claude-sonnet-5"\n', encoding="utf-8")
    (agent / "arcrun.toml").write_text("[loop]\nmax_turns = 40\n", encoding="utf-8")

    ws = agent / "workspace"
    ws.mkdir()
    (ws / "identity.md").write_text("# persona\n", encoding="utf-8")
    (ws / "context.md").write_text("# context\n", encoding="utf-8")
    (ws / "policy.md").write_text("# policy\n", encoding="utf-8")
    (ws / "scratch.md").write_text("# scratch note\n", encoding="utf-8")

    mem = ws / "memory"
    mem.mkdir()
    (mem / "daily-log.md").write_text("2026-08-29: did stuff\n", encoding="utf-8")

    sessions = ws / "sessions"
    sessions.mkdir()
    (sessions / "run-1.jsonl").write_text('{"role": "user"}\n', encoding="utf-8")

    ws_audit = ws / "audit"
    ws_audit.mkdir()
    (ws_audit / "policy-chain.jsonl").write_text('{"seq": 1}\n', encoding="utf-8")

    sibling_audit = agent / ".audit"
    sibling_audit.mkdir()
    (sibling_audit / "trace-checkpoint.worm").write_text("chain-head\n", encoding="utf-8")

    # arcprompt COMP-007 signed overlay tree — <agent_root>/context/<package>/<name>.md
    # (+ detached .arcsig sidecar), outside workspace/, reachable via root=agent.
    overlay_dir = agent / "context" / "arcagent"
    overlay_dir.mkdir(parents=True)
    (overlay_dir / "system.md").write_text("# operator override\n", encoding="utf-8")
    (overlay_dir / "system.md.arcsig").write_text("sig-bytes", encoding="utf-8")

    # Private key material. Default identity.key_dir (~/.arcagent/keys) lives
    # outside the agent root entirely; this simulates the defense-in-depth
    # case (a misconfigured key_dir, or any *.key file that ends up under the
    # agent tree) that the suffix-based block must still catch.
    (agent / "did_arc_alpha.key").write_text("ed25519-seed-bytes", encoding="utf-8")

    return root


def _build_app(team_root: Path) -> tuple[Starlette, AuthConfig]:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "op"})
    registry = AgentRegistry()
    app = Starlette(routes=[*agent_routes, *agent_detail_routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = registry
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.session_tracker = SessionTracker()
    app.state.team_root = team_root

    def _roster_provider() -> list[team_roster.RosterEntry]:
        online = {a.agent_id for a in registry.list_agents()}
        return team_roster.list_team(team_root=team_root, online_ids=online)

    app.state.roster_provider = _roster_provider
    return app, auth


@pytest.fixture
def ctx(tmp_path: Path) -> tuple[TestClient, Path]:
    team_root = _build_team_dir(tmp_path)
    app, _ = _build_app(team_root)
    return TestClient(app), team_root / "alpha_agent"


def _op() -> dict[str, str]:
    return {"Authorization": "Bearer op"}


def _viewer() -> dict[str, str]:
    return {"Authorization": "Bearer viewer"}


def _delete(
    client: TestClient, root: str, path: str, *, headers: dict[str, str], **params: str
) -> object:
    query = f"root={root}&path={path}"
    for key, value in params.items():
        query += f"&{key}={value}"
    return client.request("DELETE", f"/api/agents/alpha/files/read?{query}", headers=headers)


class TestOrdinaryDelete:
    def test_operator_deletes_ordinary_file(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = _delete(client, "workspace", "scratch.md", headers=_op())
        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == "scratch.md"
        assert body["protected"] is None
        assert not (agent_dir / "workspace" / "scratch.md").exists()

    def test_deletes_sidecar_signature_too(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        cap = agent_dir / "workspace" / "signed.md"
        cap.write_text("# signed\n", encoding="utf-8")
        (agent_dir / "workspace" / "signed.md.arcsig").write_text("sig", encoding="utf-8")

        resp = _delete(client, "workspace", "signed.md", headers=_op())
        assert resp.status_code == 200
        assert not cap.exists()
        assert not (agent_dir / "workspace" / "signed.md.arcsig").exists()

    def test_missing_file_404(self, ctx: tuple[TestClient, Path]) -> None:
        client, _ = ctx
        resp = _delete(client, "workspace", "nope.md", headers=_op())
        assert resp.status_code == 404

    def test_directory_not_deletable(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        # `memory` alone (no confirm) hits the confirmation gate first (409);
        # a directory can never reach the unlink path regardless.
        resp = _delete(client, "workspace", "memory", headers=_op(), confirm_protected="true")
        assert resp.status_code == 404
        assert (agent_dir / "workspace" / "memory").is_dir()

    def test_audits_applied(
        self, ctx: tuple[TestClient, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        client, _ = ctx
        with caplog.at_level("INFO", logger="arcui.audit"):
            _delete(client, "workspace", "scratch.md", headers=_op())
        events = [
            json.loads(r.message)
            for r in caplog.records
            if r.name == "arcui.audit" and '"ui.mutation"' in r.message
        ]
        assert any(
            e["details"]["operation"] == "file_delete"
            and e["details"]["outcome"] == "applied"
            and e["details"]["target"] == "workspace:scratch.md"
            for e in events
        )


class TestGuards:
    def test_viewer_forbidden(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = _delete(client, "workspace", "scratch.md", headers=_viewer())
        assert resp.status_code == 403
        assert (agent_dir / "workspace" / "scratch.md").exists()

    @pytest.mark.parametrize(
        "bad_path",
        ["../../etc/passwd", "/etc/passwd", "../arcagent.toml", "subdir/../../escape"],
    )
    def test_path_escape_400(self, ctx: tuple[TestClient, Path], bad_path: str) -> None:
        client, _ = ctx
        resp = _delete(client, "workspace", bad_path, headers=_op())
        assert resp.status_code == 400


class TestBlockedAgentState:
    """Identity + the audit chain: 403, no override, regardless of confirm_protected."""

    def test_identity_blocked(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = _delete(client, "workspace", "identity.md", headers=_op())
        assert resp.status_code == 403
        assert (agent_dir / "workspace" / "identity.md").exists()

    def test_identity_blocked_even_with_confirm(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = _delete(client, "workspace", "identity.md", headers=_op(), confirm_protected="true")
        assert resp.status_code == 403
        assert (agent_dir / "workspace" / "identity.md").exists()

    def test_workspace_audit_chain_blocked(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = _delete(
            client,
            "workspace",
            "audit/policy-chain.jsonl",
            headers=_op(),
            confirm_protected="true",
        )
        assert resp.status_code == 403
        assert (agent_dir / "workspace" / "audit" / "policy-chain.jsonl").exists()

    def test_sibling_dot_audit_chain_blocked_via_agent_root(
        self, ctx: tuple[TestClient, Path]
    ) -> None:
        client, agent_dir = ctx
        resp = _delete(
            client,
            "agent",
            ".audit/trace-checkpoint.worm",
            headers=_op(),
            confirm_protected="true",
        )
        assert resp.status_code == 403
        assert (agent_dir / ".audit" / "trace-checkpoint.worm").exists()

    def test_policy_blocked_not_confirm(self, ctx: tuple[TestClient, Path]) -> None:
        """Security-review fix: policy.md is an authorization DOWNGRADE if deleted
        (a PolicyPipeline with no configured file default-ALLOWs), not agent
        operating state — 403 outright, never a 409-then-confirm."""
        client, agent_dir = ctx
        resp = _delete(client, "workspace", "policy.md", headers=_op())
        assert resp.status_code == 403
        assert (agent_dir / "workspace" / "policy.md").exists()

    def test_policy_blocked_even_with_confirm(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = _delete(client, "workspace", "policy.md", headers=_op(), confirm_protected="true")
        assert resp.status_code == 403
        assert (agent_dir / "workspace" / "policy.md").exists()

    def test_blocked_delete_is_audited_denied(
        self, ctx: tuple[TestClient, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        client, _ = ctx
        with caplog.at_level("INFO", logger="arcui.audit"):
            _delete(client, "workspace", "identity.md", headers=_op())
        events = [
            json.loads(r.message)
            for r in caplog.records
            if r.name == "arcui.audit" and '"ui.mutation"' in r.message
        ]
        assert any(
            e["details"]["operation"] == "file_delete"
            and e["details"]["outcome"] == "denied"
            and "blocked" in e["details"]["detail"]
            for e in events
        )


class TestConfirmRequiredAgentState:
    """memory/, sessions/, context.md: 409 without confirm, 200 with it."""

    @pytest.mark.parametrize(
        ("root", "path"),
        [
            ("workspace", "memory/daily-log.md"),
            ("workspace", "sessions/run-1.jsonl"),
            ("workspace", "context.md"),
        ],
    )
    def test_requires_confirmation(
        self, ctx: tuple[TestClient, Path], root: str, path: str
    ) -> None:
        client, agent_dir = ctx
        resp = _delete(client, root, path, headers=_op())
        assert resp.status_code == 409
        assert (agent_dir / "workspace" / path).exists()

    @pytest.mark.parametrize(
        ("root", "path"),
        [
            ("workspace", "memory/daily-log.md"),
            ("workspace", "sessions/run-1.jsonl"),
            ("workspace", "context.md"),
        ],
    )
    def test_confirmed_delete_succeeds(
        self, ctx: tuple[TestClient, Path], root: str, path: str
    ) -> None:
        client, agent_dir = ctx
        resp = _delete(client, root, path, headers=_op(), confirm_protected="true")
        assert resp.status_code == 200
        assert resp.json()["protected"] == "confirm"
        assert not (agent_dir / "workspace" / path).exists()

    def test_confirmation_ignored_for_viewer(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = _delete(
            client, "workspace", "context.md", headers=_viewer(), confirm_protected="true"
        )
        assert resp.status_code == 403
        assert (agent_dir / "workspace" / "context.md").exists()

    def test_unconfirmed_denial_is_audited(
        self, ctx: tuple[TestClient, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        client, _ = ctx
        with caplog.at_level("INFO", logger="arcui.audit"):
            _delete(client, "workspace", "context.md", headers=_op())
        events = [
            json.loads(r.message)
            for r in caplog.records
            if r.name == "arcui.audit" and '"ui.mutation"' in r.message
        ]
        assert any(
            e["details"]["operation"] == "file_delete"
            and e["details"]["outcome"] == "denied"
            and "confirmation_required" in e["details"]["detail"]
            for e in events
        )


class TestBlockedConfigAndControlPlane:
    """Security-review hardening: config TOMLs, signed overlays, and key
    material are blocked outright — proven, not assumed (build-principles.md:
    "policy layers, prompts and keys are protected control-plane artifacts")."""

    @pytest.mark.parametrize("filename", ["arcagent.toml", "arcllm.toml", "arcrun.toml"])
    def test_config_toml_blocked(self, ctx: tuple[TestClient, Path], filename: str) -> None:
        client, agent_dir = ctx
        resp = _delete(client, "agent", filename, headers=_op(), confirm_protected="true")
        assert resp.status_code == 403
        assert (agent_dir / filename).exists()

    def test_signed_overlay_blocked(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = _delete(
            client, "agent", "context/arcagent/system.md", headers=_op(), confirm_protected="true"
        )
        assert resp.status_code == 403
        assert (agent_dir / "context" / "arcagent" / "system.md").exists()

    def test_signed_overlay_sidecar_blocked(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = _delete(
            client,
            "agent",
            "context/arcagent/system.md.arcsig",
            headers=_op(),
            confirm_protected="true",
        )
        assert resp.status_code == 403
        assert (agent_dir / "context" / "arcagent" / "system.md.arcsig").exists()

    def test_arcsig_sidecar_blocked_anywhere(self, ctx: tuple[TestClient, Path]) -> None:
        """.arcsig is blocked by suffix, not just inside context/ — the write
        route's own sidecar convention (workspace/signed.md.arcsig) is covered too."""
        client, agent_dir = ctx
        sidecar = agent_dir / "workspace" / "signed.md.arcsig"
        sidecar.write_text("sig", encoding="utf-8")
        resp = _delete(
            client, "workspace", "signed.md.arcsig", headers=_op(), confirm_protected="true"
        )
        assert resp.status_code == 403
        assert sidecar.exists()

    def test_key_file_blocked(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = _delete(
            client, "agent", "did_arc_alpha.key", headers=_op(), confirm_protected="true"
        )
        assert resp.status_code == 403
        assert (agent_dir / "did_arc_alpha.key").exists()

    def test_key_file_blocked_under_workspace_too(self, ctx: tuple[TestClient, Path]) -> None:
        """Suffix-based, not location-based: a stray .key file anywhere under
        the agent root is blocked, defense-in-depth against a misconfigured
        identity.key_dir ever resolving inside the agent tree."""
        client, agent_dir = ctx
        stray = agent_dir / "workspace" / "operator.key"
        stray.write_text("ed25519-seed-bytes", encoding="utf-8")
        resp = _delete(
            client, "workspace", "operator.key", headers=_op(), confirm_protected="true"
        )
        assert resp.status_code == 403
        assert stray.exists()

    def test_config_toml_delete_is_audited_denied(
        self, ctx: tuple[TestClient, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        client, _ = ctx
        with caplog.at_level("INFO", logger="arcui.audit"):
            _delete(client, "agent", "arcagent.toml", headers=_op())
        events = [
            json.loads(r.message)
            for r in caplog.records
            if r.name == "arcui.audit" and '"ui.mutation"' in r.message
        ]
        assert any(
            e["details"]["operation"] == "file_delete"
            and e["details"]["outcome"] == "denied"
            and "blocked" in e["details"]["detail"]
            for e in events
        )


class TestSymlinkAndToctou:
    """Security-review hardening: the protected-set check AND the unlink must
    operate on the resolved realpath, resolved ONCE — a symlink disguising a
    protected or out-of-bounds target must be refused, not followed."""

    def test_symlink_to_protected_target_refused(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        link = agent_dir / "workspace" / "notes.md"
        target = agent_dir / "workspace" / "audit" / "policy-chain.jsonl"
        link.symlink_to(target)

        resp = _delete(client, "workspace", "notes.md", headers=_op(), confirm_protected="true")

        assert resp.status_code == 403
        # Neither the symlink nor the real audit-chain file it points at was removed.
        assert link.is_symlink()
        assert target.exists()

    def test_symlink_to_outside_agent_root_refused(
        self, ctx: tuple[TestClient, Path], tmp_path: Path
    ) -> None:
        client, agent_dir = ctx
        outside = tmp_path / "outside.txt"
        outside.write_text("not this agent's file\n", encoding="utf-8")
        link = agent_dir / "workspace" / "escape.md"
        link.symlink_to(outside)

        resp = _delete(client, "workspace", "escape.md", headers=_op())

        assert resp.status_code == 400
        assert link.is_symlink()
        assert outside.exists()

    def test_symlink_to_ordinary_file_within_bounds_deletes_the_real_target(
        self, ctx: tuple[TestClient, Path]
    ) -> None:
        """A symlink to an ORDINARY (unprotected) in-bounds file is not a
        security boundary — the resolved realpath is what gets judged and
        removed, consistent with "resolve once, act on that same target"."""
        client, agent_dir = ctx
        real = agent_dir / "workspace" / "real.md"
        real.write_text("# real content\n", encoding="utf-8")
        link = agent_dir / "workspace" / "alias.md"
        link.symlink_to(real)

        resp = _delete(client, "workspace", "alias.md", headers=_op())

        assert resp.status_code == 200
        assert not real.exists()

    def test_symlink_denial_audits_resolved_path_not_request_string(
        self, ctx: tuple[TestClient, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """The audit trail must show what the symlink actually pointed at
        (workspace/audit/policy-chain.jsonl), not the innocuous request
        string (notes.md) — otherwise an incident review can't see the real
        target of a symlink-disguised delete attempt."""
        client, agent_dir = ctx
        link = agent_dir / "workspace" / "notes.md"
        target = agent_dir / "workspace" / "audit" / "policy-chain.jsonl"
        link.symlink_to(target)

        with caplog.at_level("INFO", logger="arcui.audit"):
            _delete(client, "workspace", "notes.md", headers=_op(), confirm_protected="true")

        events = [
            json.loads(r.message)
            for r in caplog.records
            if r.name == "arcui.audit" and '"ui.mutation"' in r.message
        ]
        matches = [e for e in events if e["details"]["operation"] == "file_delete"]
        assert matches, "expected at least one file_delete audit event"
        assert any(
            e["details"]["outcome"] == "denied"
            and e["details"]["target"] == "workspace:audit/policy-chain.jsonl"
            for e in matches
        )
        # The raw request string never appears as a target — only the
        # resolved reality does.
        assert not any(e["details"]["target"] == "workspace:notes.md" for e in matches)

    def test_escape_attempt_audits_resolved_path(
        self, ctx: tuple[TestClient, Path], caplog: pytest.LogCaptureFixture, tmp_path: Path
    ) -> None:
        client, agent_dir = ctx
        outside = tmp_path / "outside.txt"
        outside.write_text("not this agent's file\n", encoding="utf-8")
        link = agent_dir / "workspace" / "escape.md"
        link.symlink_to(outside)

        with caplog.at_level("INFO", logger="arcui.audit"):
            _delete(client, "workspace", "escape.md", headers=_op())

        events = [
            json.loads(r.message)
            for r in caplog.records
            if r.name == "arcui.audit" and '"ui.mutation"' in r.message
        ]
        matches = [e for e in events if e["details"]["operation"] == "file_delete"]
        assert any(
            e["details"]["outcome"] == "denied"
            and e["details"]["target"].endswith(str(outside.resolve()))
            for e in matches
        )
