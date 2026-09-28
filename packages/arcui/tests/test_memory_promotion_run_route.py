"""SPEC-083 T-1224 (COMP-029, REQ-512) — "Run now" from ArcUI.

RED intent: the route ``POST /api/agents/{id}/memory/promotion/run`` does not
exist yet, so every request 404/405s — the feature is absent.

Contract assumed::

    POST /api/agents/{id}/memory/promotion/run     (operator only)
        body: {} or {"max_items": int 1..5000}     (no other field)
        -> 200 {"status", "evaluated", "promoted", "kept_private",
                "blocked_secret", "too_large", "deferred"}   (status + counts only)
        -> 403 viewer; 404 unknown agent; 400 non-object body;
           422 bad max_items or unknown field; 503 agent not running here or
           its memory cannot run promotion; >=500 with a generic message when
           the run itself crashes.

Channel: the serve process holds the RUNNING agents in
``app.state.embedded_agent_cache`` (DID -> started ``ArcAgent``). The route
resolves the agent id to its DID through the roster and asks THAT instance —
it never builds a second, offline agent. Agent-side seam assumed:

    await agent.run_memory_promotion(max_items: int | None) -> Mapping[str, object]

(the arcagent side forwards to ``ArcMemoryBrain.run_promotion``; the per-agent
sweep lock lives in arcmemory — see
``packages/arcmemory/tests/promotion/test_manual_run.py``).

Overlap decision: the route WAITS for a nightly sweep already in progress (the
brain's per-agent lock serializes them) and then returns THIS run's result —
typically ``evaluated: 0`` because the nightly run already judged everything.
It does not return 409. Rationale: one serialization rule in one place
(arcmemory), no second "busy" contract, and the operator always gets a result.

Every accepted run is audited as ``memory.promotion.manual_run`` with the actor
(role/session on the operator-signed WORM chain), the agent and the cap — never
memory content.

Harness mirrors ``test_memory_promotion_route.py``: real Starlette app, real
``AuthMiddleware``, real agent dir + roster provider; the running agent is the
only fake (it stands in for the in-process ``ArcAgent``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from arcgateway import team_roster
from arctrust.identity import AgentIdentity
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_routes

MANUAL_RUN = "memory.promotion.manual_run"
OPERATOR_DID = "did:arc:local:operator/feedface"
RESULT_KEYS = {
    "status",
    "evaluated",
    "promoted",
    "kept_private",
    "blocked_secret",
    "too_large",
    "deferred",
}
RESULT = {
    "status": "completed",
    "evaluated": 12,
    "promoted": 3,
    "kept_private": 7,
    "blocked_secret": 1,
    "too_large": 1,
    "deferred": 40,
}
#: A memory statement the running agent holds; it must never reach the audit.
MEMORY_TEXT = "Operator's son has a clinic visit Tuesday"


# ---------------------------------------------------------------------------
# Audit capture — a stand-in for app.state.audit_worm (MutationWormWriter)
# ---------------------------------------------------------------------------


@dataclass
class _RecordingSink:
    events: list[Any] = field(default_factory=list)

    def write(self, event: Any) -> None:
        self.events.append(event)


@dataclass
class _RecordingWorm:
    """Duck-types MutationWormWriter: ``.sink``, ``.operator_did``, ``.write``."""

    sink: _RecordingSink = field(default_factory=_RecordingSink)
    operator_did: str = OPERATOR_DID
    fields: list[Any] = field(default_factory=list)

    def write(self, fields: Any) -> None:
        self.fields.append(fields)

    def records(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in [*self.sink.events, *self.fields]:
            dumped = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
            action = dumped.get("action") or dumped.get("operation")
            out.append({"action": action, "record": dumped})
        return out

    def manual_runs(self) -> list[dict[str, Any]]:
        return [r["record"] for r in self.records() if r["action"] == MANUAL_RUN]

    def dump(self) -> str:
        return json.dumps([r["record"] for r in self.records()], default=str)


# ---------------------------------------------------------------------------
# The running agent — the only fake
# ---------------------------------------------------------------------------


@dataclass
class _RunningAgent:
    """Stands in for the started, in-process ``ArcAgent`` of this DID."""

    result: dict[str, Any] = field(default_factory=lambda: dict(RESULT))
    error: BaseException | None = None
    calls: list[int | None] = field(default_factory=list)

    async def run_memory_promotion(self, *, max_items: int | None = None) -> dict[str, Any]:
        self.calls.append(max_items)
        if self.error is not None:
            raise self.error
        return dict(self.result)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    client: TestClient
    agent_id: str
    did: str
    worm: _RecordingWorm
    app: Starlette

    def set_running(self, agent: Any | None) -> None:
        self.app.state.embedded_agent_cache = {} if agent is None else {self.did: agent}


def _write_agent(tmp_path: Path, *, tier: str) -> tuple[Path, str]:
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    key_dir = tmp_path / "keys"
    identity.save_keys(key_dir)
    agent_dir = tmp_path / "team" / "olivia_agent"
    (agent_dir / "workspace").mkdir(parents=True)
    body = (
        f'[agent]\nname = "olivia"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n'
        f'[llm]\nmodel = "test/model"\n[security]\ntier = "{tier}"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n'
        "[modules.memory]\nenabled = true\npriority = 100\n"
        f'[modules.memory.config]\nbrain = "arcmemory"\ntier = "{tier}"\n'
        "[modules.memory.config.promotion]\nenabled = true\n"
    )
    (agent_dir / "arcagent.toml").write_text(body, encoding="utf-8")
    return agent_dir, identity.did


def _harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, tier: str = "personal"
) -> tuple[Harness, _RunningAgent]:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    agent_dir, did = _write_agent(tmp_path, tier=tier)
    team_root = agent_dir.parent

    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=[*agent_routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = AgentRegistry()
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    worm = _RecordingWorm()
    app.state.audit_worm = worm
    running = _RunningAgent()
    harness = Harness(TestClient(app), "olivia", did, worm, app)
    harness.set_running(running)
    return harness, running


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Harness, _RunningAgent]:
    return _harness(tmp_path, monkeypatch)


def _url(h: Harness, agent_id: str | None = None) -> str:
    return f"/api/agents/{agent_id or h.agent_id}/memory/promotion/run"


def _post(
    h: Harness,
    body: Any = None,
    *,
    token: str = "operator",
    agent_id: str | None = None,
    raw: bytes | None = None,
) -> Any:
    headers = {"Authorization": f"Bearer {token}"}
    if raw is not None:
        return h.client.post(
            _url(h, agent_id),
            content=raw,
            headers={**headers, "Content-Type": "application/json"},
        )
    return h.client.post(_url(h, agent_id), json={} if body is None else body, headers=headers)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_operator_run_asks_the_running_agent_and_returns_its_result(world: Any) -> None:
    h, running = world

    resp = _post(h)

    assert resp.status_code == 200, resp.text
    assert resp.json() == RESULT
    assert running.calls == [None]


def test_max_items_is_forwarded_for_this_run(world: Any) -> None:
    h, running = world

    resp = _post(h, {"max_items": 2000})

    assert resp.status_code == 200, resp.text
    assert running.calls == [2000]


def test_max_items_at_the_ceiling_is_accepted(world: Any) -> None:
    h, running = world

    assert _post(h, {"max_items": 5000}).status_code == 200
    assert running.calls == [5000]


def test_response_carries_status_and_counts_only(world: Any) -> None:
    """Whatever else the agent side returns never leaks through the route."""
    h, running = world
    running.result = {**RESULT, "item_ids": ["family-health"], "statement": MEMORY_TEXT}

    resp = _post(h)

    assert resp.status_code == 200
    assert set(resp.json()) == RESULT_KEYS
    assert MEMORY_TEXT not in resp.text


@pytest.mark.parametrize("status", ["tier_forbidden", "disabled", "classifier_unavailable"])
def test_non_completed_status_is_a_result_not_an_error(world: Any, status: str) -> None:
    h, running = world
    running.result = {**{k: 0 for k in RESULT_KEYS - {"status"}}, "status": status}

    resp = _post(h)

    assert resp.status_code == 200
    assert resp.json()["status"] == status
    assert resp.json()["evaluated"] == 0


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_run_is_audited_once_with_agent_and_cap(world: Any) -> None:
    h, _ = world

    assert _post(h, {"max_items": 2000}).status_code == 200

    records = h.worm.manual_runs()
    assert len(records) == 1
    record = records[0]
    assert record["actor_role"] == "operator"
    assert record["target"] == "agent:olivia"
    assert record["outcome"] == "applied"
    assert "2000" in record["detail"]
    assert "max_items" in record["detail"]


def test_audit_never_carries_memory_content(world: Any) -> None:
    h, running = world
    running.result = {**RESULT, "statement": MEMORY_TEXT}

    assert _post(h).status_code == 200

    assert h.worm.manual_runs(), "precondition: the run was audited"
    assert MEMORY_TEXT not in h.worm.dump()


# ---------------------------------------------------------------------------
# Refusals — each leaves the running agent untouched
# ---------------------------------------------------------------------------


def test_viewer_is_forbidden_and_the_agent_is_not_asked(world: Any) -> None:
    h, running = world

    resp = _post(h, token="viewer")

    assert resp.status_code == 403
    assert running.calls == []
    assert h.worm.manual_runs() == [] or all(
        r["outcome"] != "applied" for r in h.worm.manual_runs()
    )


def test_unauthenticated_request_is_refused(world: Any) -> None:
    h, running = world
    assert _post(h).status_code == 200, "precondition: the route exists"
    running.calls.clear()

    resp = h.client.post(_url(h), json={})

    assert resp.status_code in (401, 403)
    assert running.calls == []


def test_get_is_not_a_trigger(world: Any) -> None:
    """A link, prefetch or image tag must never start an egress run."""
    h, running = world
    assert _post(h).status_code == 200, "precondition: the route exists"
    running.calls.clear()

    resp = h.client.get(_url(h), headers={"Authorization": "Bearer operator"})

    assert resp.status_code in (404, 405)
    assert running.calls == []


def test_unknown_agent_is_404(world: Any) -> None:
    h, running = world
    assert _post(h).status_code == 200, "precondition: the route exists"
    running.calls.clear()

    resp = _post(h, agent_id="nobody")

    assert resp.status_code == 404
    assert running.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"max_items": 0},
        {"max_items": -1},
        {"max_items": 5001},
        {"max_items": "10"},
        {"max_items": 2.5},
        {"max_items": True},
        {"max_items": 10, "confidence_threshold": 0.5},
        {"enabled": True},
    ],
    ids=["zero", "negative", "over-ceiling", "string", "float", "bool", "extra-field", "settings"],
)
def test_bad_body_is_422_and_the_agent_is_not_asked(world: Any, body: dict[str, Any]) -> None:
    h, running = world

    resp = _post(h, body)

    assert resp.status_code == 422, resp.text
    assert running.calls == []
    assert all(r["outcome"] != "applied" for r in h.worm.manual_runs())


@pytest.mark.parametrize(
    "raw", [b"[1, 2]", b'"run"', b"{not json"], ids=["list", "str", "garbage"]
)
def test_non_object_body_is_400(world: Any, raw: bytes) -> None:
    h, running = world

    resp = _post(h, raw=raw)

    assert resp.status_code == 400
    assert running.calls == []


def test_agent_not_running_here_is_503_and_nothing_is_built(world: Any) -> None:
    """The route never starts a second, offline copy of the agent to run the sweep."""
    h, _ = world
    h.set_running(None)

    resp = _post(h)

    assert resp.status_code == 503
    assert "running" in resp.json().get("error", "").lower()


def test_no_embedded_agent_cache_is_503(world: Any) -> None:
    h, _ = world
    h.app.state.embedded_agent_cache = None

    assert _post(h).status_code == 503


def test_running_agent_without_promotion_support_is_503(world: Any) -> None:
    h, _ = world
    h.set_running(object())

    resp = _post(h)

    assert resp.status_code == 503


def test_a_crashing_run_is_a_generic_5xx_that_leaks_nothing(world: Any) -> None:
    h, running = world
    running.error = RuntimeError(f"boom at /srv/arc/team/olivia_agent: {MEMORY_TEXT}")

    resp = _post(h)

    assert resp.status_code >= 500
    assert MEMORY_TEXT not in resp.text
    assert "/srv/arc" not in resp.text
    assert running.calls == [None]
    assert MEMORY_TEXT not in h.worm.dump()


# ---------------------------------------------------------------------------
# Refusals are audited too (NIST AU-2 / AU-12: log denied attempts)
# ---------------------------------------------------------------------------


def test_viewer_refusal_is_audited_as_denied(world: Any) -> None:
    h, running = world

    assert _post(h, token="viewer").status_code == 403

    records = h.worm.manual_runs()
    assert len(records) == 1
    assert records[0]["outcome"] == "denied"
    assert records[0]["actor_role"] == "viewer"
    assert records[0]["target"] == "agent:olivia"
    assert running.calls == []


@pytest.mark.parametrize("body", [{"max_items": 5001}, {"max_items": "10"}, {"enabled": True}])
def test_bad_cap_refusal_is_audited_as_denied(world: Any, body: dict[str, Any]) -> None:
    h, running = world

    assert _post(h, body).status_code == 422

    records = h.worm.manual_runs()
    assert len(records) == 1
    assert records[0]["outcome"] == "denied"
    assert "max_items" in records[0]["detail"]
    assert running.calls == []


def test_crashed_run_is_audited_as_failed_without_its_message(world: Any) -> None:
    h, running = world
    running.error = RuntimeError(MEMORY_TEXT)

    assert _post(h, {"max_items": 7}).status_code >= 500

    records = h.worm.manual_runs()
    assert [r["outcome"] for r in records] == ["failed"]
    assert "7" in records[0]["detail"]
    assert MEMORY_TEXT not in h.worm.dump()
