"""Pulse add / edit / remove routes: one operator-only, audited, validated writer.

Reuses the real-app fixture of the approval routes. A write never approves: the
new or edited check reads "pending approval" and is approved through the
existing flow, by digest, as the operator saw it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcagent.modules.pulse.proposals import list_proposals, propose_pulse_check
from starlette.testclient import TestClient

from .test_pulse_approval_routes import (
    APPROVE,
    LIST,
    _audit_events,
    _checks,
    _op,
    _viewer,
    ctx,  # noqa: F401  (pytest fixture)
)

NO_DIGEST = "0" * 64


def _body(name: str = "newcheck", **over: object) -> dict[str, object]:
    return {"name": name, "interval_minutes": 30, "action": "Sweep the inbox", **over}


class TestAddCheck:
    def test_operator_adds_a_pending_check(self, ctx: tuple[TestClient, Path]) -> None:  # noqa: F811
        client, workspace = ctx
        resp = client.post(LIST, headers=_op(), json=_body())
        assert resp.status_code == 201
        check = _checks(client)["newcheck"]
        assert check["status"] == "unapproved"
        assert check["interval_minutes"] == 30
        assert "newcheck" in (workspace / "pulse.md").read_text()

    def test_added_check_can_be_approved_as_seen(self, ctx: tuple[TestClient, Path]) -> None:  # noqa: F811
        client, _ = ctx
        client.post(LIST, headers=_op(), json=_body())
        digest = _checks(client)["newcheck"]["definition_digest"]
        resp = client.post(
            APPROVE, headers=_op(), json={"check": "newcheck", "definition_digest": digest}
        )
        assert resp.status_code == 200

    def test_viewer_cannot_add_and_nothing_is_written(
        self,
        ctx: tuple[TestClient, Path],  # noqa: F811
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, workspace = ctx
        before = (workspace / "pulse.md").read_text()
        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = client.post(LIST, headers=_viewer(), json=_body())
        assert resp.status_code == 403
        assert (workspace / "pulse.md").read_text() == before
        assert _audit_events(caplog)[-1]["outcome"] == "denied"

    def test_unauthenticated_cannot_add(self, ctx: tuple[TestClient, Path]) -> None:  # noqa: F811
        client, _ = ctx
        assert client.post(LIST, json=_body()).status_code in (401, 403)

    def test_add_is_audited(
        self,
        ctx: tuple[TestClient, Path],  # noqa: F811
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, _ = ctx
        with caplog.at_level("INFO", logger="arcui.audit"):
            client.post(LIST, headers=_op(), json=_body())
        event = _audit_events(caplog)[-1]
        assert event["operation"] == "pulse.add"
        assert event["target"] == "pulse:newcheck"
        assert event["outcome"] == "applied"

    @pytest.mark.parametrize(
        "over",
        [
            {"name": "bad name"},
            {"interval_minutes": 0},
            {"interval_minutes": "5"},
            {"action": ""},
            {"action": "x **Approval:** {}"},
        ],
    )
    def test_invalid_check_is_400_and_unwritten(
        self,
        ctx: tuple[TestClient, Path],  # noqa: F811
        over: dict[str, object],
    ) -> None:
        client, workspace = ctx
        before = (workspace / "pulse.md").read_text()
        assert client.post(LIST, headers=_op(), json=_body(**over)).status_code == 400
        assert (workspace / "pulse.md").read_text() == before

    def test_duplicate_is_409(self, ctx: tuple[TestClient, Path]) -> None:  # noqa: F811
        client, _ = ctx
        assert client.post(LIST, headers=_op(), json=_body("health")).status_code == 409

    def test_unknown_agent_is_404(self, ctx: tuple[TestClient, Path]) -> None:  # noqa: F811
        client, _ = ctx
        resp = client.post("/api/agents/ghost/pulse", headers=_op(), json=_body())
        assert resp.status_code == 404


class TestEditAndRemove:
    def _digest(self, client: TestClient, name: str = "health") -> str:
        return _checks(client)[name]["definition_digest"]

    def test_edit_turns_an_approved_check_into_changes_pending(
        self,
        ctx: tuple[TestClient, Path],  # noqa: F811
    ) -> None:
        client, _ = ctx
        digest = self._digest(client)
        client.post(APPROVE, headers=_op(), json={"check": "health", "definition_digest": digest})
        resp = client.put(
            f"{LIST}/health",
            headers=_op(),
            json={"interval_minutes": 15, "action": "Deeper check", "definition_digest": digest},
        )
        assert resp.status_code == 200
        check = _checks(client)["health"]
        assert check["status"] == "changes_pending"
        assert check["approved"] is False
        assert "Deeper check" in check["diff"]

    def test_edit_of_text_the_operator_did_not_see_is_409(
        self,
        ctx: tuple[TestClient, Path],  # noqa: F811
    ) -> None:
        client, _ = ctx
        resp = client.put(
            f"{LIST}/health",
            headers=_op(),
            json={"interval_minutes": 15, "action": "x", "definition_digest": NO_DIGEST},
        )
        assert resp.status_code == 409

    def test_viewer_cannot_edit_or_remove(self, ctx: tuple[TestClient, Path]) -> None:  # noqa: F811
        client, workspace = ctx
        before = (workspace / "pulse.md").read_text()
        digest = self._digest(client)
        put = client.put(
            f"{LIST}/health",
            headers=_viewer(),
            json={"interval_minutes": 15, "action": "x", "definition_digest": digest},
        )
        delete = client.request(
            "DELETE", f"{LIST}/health", headers=_viewer(), json={"definition_digest": digest}
        )
        assert (put.status_code, delete.status_code) == (403, 403)
        assert (workspace / "pulse.md").read_text() == before

    def test_remove_deletes_the_check_and_audits(
        self,
        ctx: tuple[TestClient, Path],  # noqa: F811
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, _ = ctx
        digest = self._digest(client, "inbox")
        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = client.request(
                "DELETE", f"{LIST}/inbox", headers=_op(), json={"definition_digest": digest}
            )
        assert resp.status_code == 200
        assert set(_checks(client)) == {"health"}
        event = _audit_events(caplog)[-1]
        assert (event["operation"], event["outcome"]) == ("pulse.remove", "applied")

    def test_edit_or_remove_unknown_is_404(self, ctx: tuple[TestClient, Path]) -> None:  # noqa: F811
        client, _ = ctx
        put = client.put(
            f"{LIST}/ghost",
            headers=_op(),
            json={"interval_minutes": 5, "action": "x", "definition_digest": NO_DIGEST},
        )
        delete = client.request(
            "DELETE", f"{LIST}/ghost", headers=_op(), json={"definition_digest": NO_DIGEST}
        )
        assert (put.status_code, delete.status_code) == (404, 404)


class TestProposals:
    def test_listing_carries_pending_proposals(self, ctx: tuple[TestClient, Path]) -> None:  # noqa: F811
        client, workspace = ctx
        propose_pulse_check(
            workspace, name="idea", interval_minutes=60, action="Look", reason="because"
        )
        body = client.get(LIST, headers=_viewer()).json()
        assert [p["name"] for p in body["proposals"]] == ["idea"]

    def test_adding_with_proposal_clears_it(self, ctx: tuple[TestClient, Path]) -> None:  # noqa: F811
        client, workspace = ctx
        propose_pulse_check(workspace, name="idea", interval_minutes=60, action="Look", reason="")
        resp = client.post(LIST, headers=_op(), json=_body("idea", proposal="idea"))
        assert resp.status_code == 201
        assert list_proposals(workspace) == []

    def test_operator_can_dismiss_a_proposal_viewer_cannot(
        self,
        ctx: tuple[TestClient, Path],  # noqa: F811
    ) -> None:
        client, workspace = ctx
        propose_pulse_check(workspace, name="idea", interval_minutes=60, action="Look", reason="")
        url = f"{LIST}/proposals/idea"
        assert client.delete(url, headers=_viewer()).status_code == 403
        assert list_proposals(workspace)
        assert client.delete(url, headers=_op()).status_code == 200
        assert list_proposals(workspace) == []
