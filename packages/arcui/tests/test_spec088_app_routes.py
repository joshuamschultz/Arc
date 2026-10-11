"""SPEC-088 T-1330: the app route table gains this spec's rows, and only those.

``AuthMiddleware`` refuses any app call whose method and path match no
``APP_ROUTES`` row (SPEC-087 COMP-005). These tests pin the SPEC-088 rows
(COMP-001, COMP-011, COMP-014), their scope column, and the paths that must
stay unreachable to apps: the run list and every callback-management route.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packages.arcui.tests.principal_fixtures import (
    agent_did,
    build_principal_app,
    issue_principal,
    principal_rig,
    write_team,
)
from packages.arcui.tests.user_authority import user_store_factory
from starlette.testclient import TestClient

from arcui.principals import Scope, is_hard_denied, match_app_route

# (method, concrete path, expected scope column). ``None``: the handler resolves
# the agent (body, intake row, bundle owner or ``agent`` query) and calls
# ``require_scope`` itself, so the middleware does no scope check for the row.
SPEC088_ROWS: tuple[tuple[str, str, Scope | None], ...] = (
    ("POST", "/api/runs", None),
    ("GET", "/api/runs/run-123", None),
    ("GET", "/api/runs/run-123/audit", Scope.RUNS_READ),
    ("GET", "/api/runs/run-123/recalls", Scope.RUNS_READ),
    ("POST", "/api/workflows/wf-1/events/order.placed", None),
    ("GET", "/api/work-events/stream", None),
    ("GET", "/api/work-events", None),
    ("GET", "/api/work-events/keys", None),
)

UNREACHABLE: tuple[tuple[str, str], ...] = (
    ("GET", "/api/runs"),
    ("GET", "/api/runs/"),
    ("PATCH", "/api/runs/run-123"),
    ("DELETE", "/api/runs/run-123"),
    ("POST", "/api/runs/run-123"),
    ("GET", "/api/runs/run-123/extra"),
    ("GET", "/api/runs/run-123/audit/x"),
    ("POST", "/api/runs/run-123/audit"),
    ("GET", "/api/work-events/callbacks"),
    ("POST", "/api/work-events/callbacks"),
    ("POST", "/api/work-events/callbacks/x/remove"),
    ("DELETE", "/api/work-events/callbacks/x"),
    ("GET", "/api/work-events/callbacks/x"),
    ("GET", "/api/work-events/streamx"),
    ("GET", "/api/work-events/keys/rotate"),
    ("POST", "/api/work-events"),
    ("POST", "/api/work-events/stream"),
    ("GET", "/api/workflows/wf-1/events/order.placed"),
    ("POST", "/api/workflows/wf-1/events"),
    ("POST", "/api/workflows/wf-1/events/a/b"),
    ("POST", "/api/workflows/wf-1/x/events/a"),
)


@pytest.mark.parametrize(("method", "path", "scope"), SPEC088_ROWS)
def test_spec088_row_matches_with_expected_scope(
    method: str, path: str, scope: Scope | None
) -> None:
    route = match_app_route(method, path)
    assert route is not None, f"no APP_ROUTES row for {method} {path}"
    assert route.scope == scope
    # No SPEC-088 agent is named in the path: every handler binds it itself.
    assert route.agent_from_path is None
    assert not is_hard_denied(path)


@pytest.mark.parametrize(("method", "path"), UNREACHABLE)
def test_paths_outside_the_spec088_rows_match_nothing(method: str, path: str) -> None:
    assert match_app_route(method, path) is None


def test_existing_timeline_row_still_matches() -> None:
    route = match_app_route("GET", "/api/runs/run-123/timeline")
    assert route is not None
    assert route.scope == Scope.RUNS_READ


class TestRunReadRowsAreHandlerBound:
    """A row the middleware does not agent-check opens its handler to every app.

    ``/api/runs/{id}/audit`` and ``/recalls`` carry ``Scope.RUNS_READ`` like the
    timeline row, but ``agent_from_path`` is None, so the middleware never checks
    it: the handler must resolve the run's agent and refuse (SDD COMP-001). An
    app holding no scope at all must get one audited refusal, never the rows.
    """

    @pytest.mark.parametrize("suffix", ["timeline", "audit", "recalls"])
    def test_app_without_runs_read_is_refused_with_one_audit_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
    ) -> None:
        monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
        rig = principal_rig(tmp_path / "authority")
        built = build_principal_app(
            tmp_path,
            principal_store_factory=rig.factory,
            users=user_store_factory(tmp_path / "accounts"),
            team_root=write_team(tmp_path / "team", {"olivia": agent_did()}),
            tier="personal",
        )
        app_principal = issue_principal(rig.open(), scopes={})
        client = TestClient(built.app, raise_server_exceptions=False)

        resp = client.get(f"/api/runs/r1/{suffix}", headers=app_principal.bearer_headers())

        assert resp.status_code in (403, 404), resp.text
        assert len(built.worm.denied()) == 1
