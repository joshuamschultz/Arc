"""Alpha-2 item 16 — operator share of one memory card + its decision history.

Contract::

    POST /api/agents/{id}/knowledge/{kind}/{item_id}/share      (operator only)
        body: {} (nothing else)
        -> 200 {"status": "published", "shared_ref"}
        -> 202 outcome_unknown; 403 viewer / tier_forbidden / clearance_refused;
           404 unknown agent / not_found; 409 demoted / refused / disabled;
           422 bad kind, unknown field, blocked_secret, too_large;
           503 agent not running here / no operator key / publisher_unavailable
    GET  /api/agents/{id}/knowledge/{kind}/{item_id}/decisions   (any role)
        -> 200 {"kind", "item_id", "decisions": [ledger rows, oldest first]}

``decided_by`` is the deployment operator DID derived from the operator signing
key — the browser never names it. Every share attempt is audited as
``memory.promotion.operator_share``.

Harness mirrors ``test_memory_promotion_run_route.py``: real Starlette app, real
``AuthMiddleware``, real agent dir + roster; the running agent is the only fake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from arcgateway import team_roster
from arctrust.identity import AgentIdentity, did_from_public_key
from arctrust.signer import InProcessSigner
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_routes

SHARE = "memory.promotion.operator_share"
_SIGNER = InProcessSigner(b"\x0a" * 32)
OPERATOR_DID = did_from_public_key(_SIGNER.public_key, org="operator", agent_type="approver")
HISTORY = [
    {"item_kind": "insight", "item_id": "acme", "decision": "keep_private"},
    {"item_kind": "insight", "item_id": "acme", "decision": "promoted_by_operator"},
]


@dataclass
class _RecordingWorm:
    fields: list[Any] = field(default_factory=list)

    def write(self, fields: Any) -> None:
        self.fields.append(fields)

    def shares(self) -> list[dict[str, Any]]:
        dumped = [item.model_dump(mode="json") for item in self.fields]
        return [record for record in dumped if record.get("operation") == SHARE]


@dataclass
class _RunningAgent:
    status: str = "published"
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    async def share_memory_item(
        self, kind: str, item_id: str, *, decided_by: str
    ) -> dict[str, Any]:
        self.calls.append((kind, item_id, decided_by))
        shared = "0123456789abcdef" if self.status == "published" else None
        return {"status": self.status, "shared_ref": shared}

    async def memory_decision_history(self, kind: str, item_id: str) -> list[dict[str, Any]]:
        return [row for row in HISTORY if (row["item_kind"], row["item_id"]) == (kind, item_id)]


@dataclass
class Harness:
    client: TestClient
    app: Starlette
    did: str
    worm: _RecordingWorm
    agent: _RunningAgent

    def post(
        self, path: str = "insight/acme", *, token: str = "operator", body: Any = None
    ) -> Any:
        return self.client.post(
            f"/api/agents/olivia/knowledge/{path}/share",
            json={} if body is None else body,
            headers={"Authorization": f"Bearer {token}"},
        )


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    identity.save_keys(tmp_path / "keys")
    agent_dir = tmp_path / "team" / "olivia_agent"
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(
        f'[agent]\nname = "olivia"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n[llm]\nmodel = "test/model"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{tmp_path / "keys"}"\n'
        'vault_path = ""\n',
        encoding="utf-8",
    )
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=[*agent_routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = AgentRegistry()
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=agent_dir.parent, online_ids=set()
    )
    app.state.operator_signer_factory = lambda: _SIGNER
    worm = _RecordingWorm()
    app.state.audit_worm = worm
    agent = _RunningAgent()
    app.state.embedded_agent_cache = {identity.did: agent}
    return Harness(TestClient(app), app, identity.did, worm, agent)


def test_share_records_operator_promote(h: Harness) -> None:
    resp = h.post()

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "published", "shared_ref": "0123456789abcdef"}
    assert h.agent.calls == [("insight", "acme", OPERATOR_DID)]
    [record] = h.worm.shares()
    assert record["outcome"] == "applied"
    assert OPERATOR_DID in record["detail"]
    assert record["target"] == "agent:olivia"


@pytest.mark.parametrize(
    ("status", "code"),
    [("blocked_secret", 422), ("tier_forbidden", 403), ("demoted", 409), ("too_large", 422)],
)
def test_share_refuses_secret_card_and_federal(h: Harness, status: str, code: int) -> None:
    """The agent's gates decide; the route maps each refusal and audits it as denied."""
    h.agent.status = status

    resp = h.post()

    assert resp.status_code == code
    assert resp.json() == {"status": status, "shared_ref": None}
    assert [r["outcome"] for r in h.worm.shares()] == ["denied"]


def test_viewer_cannot_share(h: Harness) -> None:
    resp = h.post(token="viewer")

    assert resp.status_code == 403
    assert h.agent.calls == []
    assert [r["outcome"] for r in h.worm.shares()] == ["denied"]


@pytest.mark.parametrize(
    ("path", "body", "code"),
    [
        ("event/acme", None, 422),
        ("insight/acme", {"decided_by": "did:arc:operator:approver/forged"}, 422),
        ("insight/acme", ["x"], 400),
    ],
    ids=["episodic-kind", "browser-named-decider", "non-object-body"],
)
def test_share_refuses_bad_requests_before_the_agent(
    h: Harness, path: str, body: Any, code: int
) -> None:
    resp = h.post(path, body=body)

    assert resp.status_code == code
    assert h.agent.calls == []


def test_share_unknown_agent_and_not_running(h: Harness) -> None:
    unknown = h.client.post(
        "/api/agents/nobody/knowledge/insight/acme/share",
        json={},
        headers={"Authorization": "Bearer operator"},
    )
    h.app.state.embedded_agent_cache = {}
    offline = h.post()

    assert unknown.status_code == 404
    assert offline.status_code == 503


def test_share_without_operator_key_is_503(h: Harness) -> None:
    def _no_key() -> Any:
        raise RuntimeError("no operator custody")

    h.app.state.operator_signer_factory = _no_key

    resp = h.post()

    assert resp.status_code == 503
    assert h.agent.calls == []
    assert [r["outcome"] for r in h.worm.shares()] == ["failed"]


def test_decisions_lists_the_card_history_for_any_role(h: Harness) -> None:
    resp = h.client.get(
        "/api/agents/olivia/knowledge/insight/acme/decisions",
        headers={"Authorization": "Bearer viewer"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"kind": "insight", "item_id": "acme", "decisions": HISTORY}
