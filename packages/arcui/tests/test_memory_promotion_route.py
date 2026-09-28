"""SPEC-083 T-1220 (COMP-028; REQ-509, REQ-510, REQ-511) — promotion settings from ArcUI.

Contract under test::

    GET /api/agents/{id}/memory/promotion
        -> 200 {"enabled": bool, "confidence_threshold": float,
                "classifier_model": str, "tier": str,
                "federal_locked": bool, "key_set": bool}
    PUT /api/agents/{id}/memory/promotion   (operator only)
        body: any subset of {"enabled", "confidence_threshold", "classifier_model"}
        -> 200 with the GET shape after the write
        -> 422 + file byte-identical on: threshold < 0.90 floor, a ``*-latest``
           model, ``enabled`` on a federal agent, or any field outside the three
        -> 403 + file byte-identical for a non-operator

The key never travels through this route: it goes to ``PUT /api/keys/TYPESAFE_API_KEY``
(the one write-only KeyStore), which must now accept that name because arcllm's
Jev drop-in declares it. GET reports only ``key_set``.

Audit is captured on a recording stand-in for the process's signed WORM writer
(``app.state.audit_worm``) — the same object ``emit_mutation_audit`` and
``operator_audit_sink`` write through — so either emission path is observed.

Harness mirrors ``test_agent_config_files_route.py`` + ``test_keys_routes.py``:
real Starlette app, real ``AuthMiddleware``, real agent dir + roster provider.
"""

from __future__ import annotations

import json
import tomllib
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
from arcui.routes.keys import routes as keys_routes

JEV_KEY = "TYPESAFE_API_KEY"
SENTINEL = "ts-zzz-web-jev-key-sentinel-8812"
CONFIG_CHANGED = "memory.promotion.config_changed"
OPERATOR_DID = "did:arc:local:operator/feedface"
SHAPE = {
    "enabled",
    "confidence_threshold",
    "classifier_model",
    "tier",
    "federal_locked",
    "key_set",
}

_PROMOTION_BLOCK = (
    "[modules.memory.config.promotion]\n"
    "enabled = false\n"
    "confidence_threshold = 0.95\n"
    'classifier_model = "jev-1.13.0"\n'
    "max_items_per_sweep = 50  # unrelated knob must survive\n"
)


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
        """Every durable record, whichever path wrote it, as plain dicts."""
        out: list[dict[str, Any]] = []
        for item in [*self.sink.events, *self.fields]:
            dumped = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
            action = dumped.get("action") or dumped.get("operation")
            out.append({"action": action, "record": dumped})
        return out

    def config_changes(self) -> list[dict[str, Any]]:
        return [r for r in self.records() if r["action"] == CONFIG_CHANGED]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    client: TestClient
    agent_id: str
    agent_dir: Path
    did: str
    worm: _RecordingWorm

    @property
    def toml_path(self) -> Path:
        return self.agent_dir / "arcagent.toml"


def _write_agent(tmp_path: Path, *, tier: str, promotion_block: str | None) -> tuple[Path, str]:
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    key_dir = tmp_path / "keys"
    identity.save_keys(key_dir)
    agent_dir = tmp_path / "team" / "olivia_agent"
    (agent_dir / "workspace").mkdir(parents=True)
    body = (
        "# operator comment that must survive a write\n"
        f'[agent]\nname = "olivia"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n'
        f'[llm]\nmodel = "test/model"\n[security]\ntier = "{tier}"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n'
        "[modules.memory]\nenabled = true\npriority = 100\n"
        f'[modules.memory.config]\nbrain = "arcmemory"\ntier = "{tier}"\n'
        "top_k = 9  # operator tuned\n"
    )
    if promotion_block is not None:
        body += promotion_block
    (agent_dir / "arcagent.toml").write_text(body, encoding="utf-8")
    return agent_dir, identity.did


def _harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tier: str = "personal",
    promotion_block: str | None = _PROMOTION_BLOCK,
) -> Harness:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    agent_dir, did = _write_agent(tmp_path, tier=tier, promotion_block=promotion_block)
    team_root = agent_dir.parent

    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=[*agent_routes, *keys_routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = AgentRegistry()
    app.state.embedded_agent_cache = None
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    worm = _RecordingWorm()
    app.state.audit_worm = worm
    return Harness(TestClient(app), "olivia", agent_dir, did, worm)


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return _harness(tmp_path, monkeypatch)


@pytest.fixture
def fed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return _harness(tmp_path, monkeypatch, tier="federal")


def _url(h: Harness, agent_id: str | None = None) -> str:
    return f"/api/agents/{agent_id or h.agent_id}/memory/promotion"


def _get(h: Harness, token: str = "operator", agent_id: str | None = None) -> Any:
    return h.client.get(_url(h, agent_id), headers={"Authorization": f"Bearer {token}"})


def _put(h: Harness, body: Any, token: str = "operator", agent_id: str | None = None) -> Any:
    return h.client.put(_url(h, agent_id), json=body, headers={"Authorization": f"Bearer {token}"})


def _put_key(h: Harness, value: str = SENTINEL, token: str = "operator") -> Any:
    return h.client.put(
        f"/api/keys/{JEV_KEY}",
        json={"value": value},
        headers={"Authorization": f"Bearer {token}"},
    )


def _promotion_on_disk(h: Harness) -> dict[str, Any]:
    doc = tomllib.loads(h.toml_path.read_text(encoding="utf-8"))
    return dict(doc["modules"]["memory"]["config"].get("promotion", {}))


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


def test_get_returns_settings_tier_and_key_flag(h: Harness) -> None:
    resp = _get(h)

    assert resp.status_code == 200
    assert resp.json() == {
        "enabled": False,
        "confidence_threshold": 0.95,
        "classifier_model": "jev-1.13.0",
        "tier": "personal",
        "federal_locked": False,
        "key_set": False,
    }


def test_get_uses_defaults_when_no_promotion_block_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _harness(tmp_path, monkeypatch, promotion_block=None)

    resp = _get(h)

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["confidence_threshold"] == 0.95
    assert body["classifier_model"] == "jev-1.13.0"


def test_get_on_a_federal_agent_reports_federal_locked(fed: Harness) -> None:
    body = _get(fed).json()
    assert body["tier"] == "federal"
    assert body["federal_locked"] is True


def test_get_for_an_unknown_agent_is_404(h: Harness) -> None:
    assert _get(h).status_code == 200, "precondition: the route exists"
    assert _get(h, agent_id="nobody").status_code == 404


def test_key_set_flips_after_put_api_keys_and_the_value_is_never_returned(h: Harness) -> None:
    put = _put_key(h)
    assert put.status_code == 200, put.text

    resp = _get(h)
    assert resp.status_code == 200
    assert resp.json()["key_set"] is True
    assert set(resp.json()) == SHAPE
    assert SENTINEL not in resp.text
    assert "ts-zzz" not in resp.text


def test_key_set_clears_after_delete_api_keys(h: Harness) -> None:
    assert _put_key(h).status_code == 200
    deleted = h.client.delete(f"/api/keys/{JEV_KEY}", headers={"Authorization": "Bearer operator"})
    assert deleted.status_code == 200
    assert _get(h).json()["key_set"] is False


# ---------------------------------------------------------------------------
# /api/keys accepts the declared Jev key, and only that name
# ---------------------------------------------------------------------------


def test_api_keys_put_accepts_the_jev_key_without_echoing_it(h: Harness) -> None:
    resp = _put_key(h)
    assert resp.status_code == 200
    assert resp.json() == {"env_var": JEV_KEY, "present": True}
    assert SENTINEL not in resp.text


def test_api_keys_lists_the_jev_key_by_presence_only(h: Harness) -> None:
    _put_key(h)
    resp = h.client.get("/api/keys", headers={"Authorization": "Bearer viewer"})
    entry = next(e for e in resp.json()["keys"] if e["env_var"] == JEV_KEY)
    assert entry["present"] is True
    assert SENTINEL not in resp.text


def test_api_keys_still_refuses_a_near_miss_name(h: Harness) -> None:
    resp = h.client.put(
        "/api/keys/TYPESAFE_BASE_URL",
        json={"value": "https://evil.example"},
        headers={"Authorization": "Bearer operator"},
    )
    assert resp.status_code == 400


def test_a_jev_key_write_is_audited_by_coordinate_never_value(h: Harness) -> None:
    assert _put_key(h).status_code == 200
    records = json.dumps(h.worm.records(), default=str)
    assert JEV_KEY in records
    assert SENTINEL not in records


# ---------------------------------------------------------------------------
# PUT — happy path
# ---------------------------------------------------------------------------


def test_put_writes_the_block_and_returns_the_new_settings(h: Harness) -> None:
    resp = _put(h, {"enabled": True, "confidence_threshold": 0.96, "classifier_model": "jev-1.14"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "enabled": True,
        "confidence_threshold": 0.96,
        "classifier_model": "jev-1.14",
        "tier": "personal",
        "federal_locked": False,
        "key_set": False,
    }
    on_disk = _promotion_on_disk(h)
    assert on_disk["enabled"] is True
    assert on_disk["confidence_threshold"] == 0.96
    assert on_disk["classifier_model"] == "jev-1.14"


def test_put_preserves_comments_and_every_other_setting(h: Harness) -> None:
    before = tomllib.loads(h.toml_path.read_text(encoding="utf-8"))

    assert _put(h, {"confidence_threshold": 0.97}).status_code == 200

    text = h.toml_path.read_text(encoding="utf-8")
    assert "# operator comment that must survive a write" in text
    assert "# operator tuned" in text
    assert "# unrelated knob must survive" in text
    after = tomllib.loads(text)
    before_promo = before["modules"]["memory"]["config"].pop("promotion")
    after_promo = after["modules"]["memory"]["config"].pop("promotion")
    assert after == before
    assert after_promo == {**before_promo, "confidence_threshold": 0.97}


def test_put_creates_the_block_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _harness(tmp_path, monkeypatch, promotion_block=None)

    assert _put(h, {"enabled": True}).status_code == 200
    assert _promotion_on_disk(h)["enabled"] is True


def test_put_accepts_the_threshold_floor_itself(h: Harness) -> None:
    assert _put(h, {"confidence_threshold": 0.90}).status_code == 200
    assert _promotion_on_disk(h)["confidence_threshold"] == 0.90


def test_a_federal_agent_may_tune_a_disabled_block(fed: Harness) -> None:
    resp = _put(fed, {"confidence_threshold": 0.97})
    assert resp.status_code == 200, resp.text
    assert _promotion_on_disk(fed) == {
        "enabled": False,
        "confidence_threshold": 0.97,
        "classifier_model": "jev-1.13.0",
        "max_items_per_sweep": 50,
    }


# ---------------------------------------------------------------------------
# PUT — refusals: 422 and the file is byte-identical, nothing applied in audit
# ---------------------------------------------------------------------------


def _assert_refused(h: Harness, resp: Any, before: bytes, status: int = 422) -> None:
    assert resp.status_code == status, resp.text
    assert h.toml_path.read_bytes() == before
    applied = [
        r
        for r in h.worm.config_changes()
        if r["record"].get("outcome") not in ("denied", "deny", "refused")
    ]
    assert applied == []


@pytest.mark.parametrize(
    "body",
    [
        {"confidence_threshold": 0.85},
        {"confidence_threshold": 0.8999},
        {"confidence_threshold": 1.5},
        {"confidence_threshold": "high"},
        {"classifier_model": "jev-latest"},
        {"classifier_model": "JEV-Latest"},
        {"classifier_model": "latest"},
        {"classifier_model": ""},
        {"enabled": True, "confidence_threshold": 0.5},
    ],
)
def test_put_invalid_settings_are_422_and_nothing_is_written(
    h: Harness, body: dict[str, Any]
) -> None:
    before = h.toml_path.read_bytes()
    _assert_refused(h, _put(h, body), before)


def test_put_enabled_on_a_federal_agent_is_422_and_nothing_is_written(fed: Harness) -> None:
    before = fed.toml_path.read_bytes()
    resp = _put(fed, {"enabled": True})
    _assert_refused(fed, resp, before)
    assert "federal" in resp.text.lower()


def test_put_enabled_is_refused_when_only_security_tier_is_federal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail closed: [security].tier is the agent's tier even if the memory block
    # forgot to repeat it.
    h = _harness(tmp_path, monkeypatch, tier="federal")
    text = h.toml_path.read_text(encoding="utf-8").replace(
        'brain = "arcmemory"\ntier = "federal"\n', 'brain = "arcmemory"\n'
    )
    h.toml_path.write_text(text, encoding="utf-8")
    before = h.toml_path.read_bytes()

    _assert_refused(h, _put(h, {"enabled": True}), before)


def test_put_on_a_tampered_federal_block_already_enabled_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The MERGED block is validated, not only the fields in this request.
    h = _harness(
        tmp_path,
        monkeypatch,
        tier="federal",
        promotion_block=_PROMOTION_BLOCK.replace("enabled = false", "enabled = true"),
    )
    before = h.toml_path.read_bytes()

    _assert_refused(h, _put(h, {"confidence_threshold": 0.97}), before)


@pytest.mark.parametrize(
    "body",
    [
        {"api_key": SENTINEL},
        {"TYPESAFE_API_KEY": SENTINEL},
        {"enabled": True, "classifier": "os:system"},
        {"max_items_per_sweep": 100000},
    ],
)
def test_put_refuses_any_field_outside_the_three_settings(
    h: Harness, body: dict[str, Any]
) -> None:
    # A key value (or any other knob) must never be writable into agent TOML here.
    before = h.toml_path.read_bytes()
    resp = _put(h, body)
    _assert_refused(h, resp, before)
    assert SENTINEL not in resp.text


def test_put_non_object_body_is_refused_without_a_write(h: Harness) -> None:
    before = h.toml_path.read_bytes()
    resp = _put(h, [1, 2, 3])
    assert resp.status_code in (400, 422)
    assert h.toml_path.read_bytes() == before


def test_put_oversized_body_is_413_without_a_write(h: Harness) -> None:
    before = h.toml_path.read_bytes()
    resp = h.client.put(
        _url(h),
        content=b'{"classifier_model": "' + b"x" * 70_000 + b'"}',
        headers={"Authorization": "Bearer operator", "Content-Type": "application/json"},
    )
    _assert_refused(h, resp, before, status=413)


def test_put_for_an_unknown_agent_is_404(h: Harness) -> None:
    assert _get(h).status_code == 200, "precondition: the route exists"
    assert _put(h, {"enabled": True}, agent_id="nobody").status_code == 404


# ---------------------------------------------------------------------------
# Operator gate
# ---------------------------------------------------------------------------


def test_put_by_a_viewer_is_403_and_nothing_is_written(h: Harness) -> None:
    before = h.toml_path.read_bytes()
    resp = _put(h, {"enabled": True}, token="viewer")
    _assert_refused(h, resp, before, status=403)


def test_put_without_a_token_is_refused_and_nothing_is_written(h: Harness) -> None:
    assert _get(h).status_code == 200, "precondition: the route exists"
    before = h.toml_path.read_bytes()
    resp = h.client.put(_url(h), json={"enabled": True})
    assert resp.status_code in (401, 403)
    assert h.toml_path.read_bytes() == before


# ---------------------------------------------------------------------------
# Audit — one durable record per accepted change, old -> new, operator + agent
# ---------------------------------------------------------------------------


def test_an_accepted_put_emits_one_config_changed_record_with_old_and_new(h: Harness) -> None:
    assert _put(h, {"enabled": True, "confidence_threshold": 0.96}).status_code == 200

    changes = h.worm.config_changes()
    assert len(changes) == 1
    record = changes[0]["record"]
    flat = json.dumps(record, default=str)
    assert h.agent_id in flat or h.did in flat
    for fragment in ("enabled", "confidence_threshold", "0.95", "0.96"):
        assert fragment in flat, fragment
    # The operator is named: either the record's actor DID, or the operator
    # role on a MutationAuditFields routed through the operator-signed writer.
    assert OPERATOR_DID in flat or record.get("actor_role") == "operator"


def test_each_accepted_put_is_its_own_record(h: Harness) -> None:
    assert _put(h, {"confidence_threshold": 0.96}).status_code == 200
    assert _put(h, {"classifier_model": "jev-1.14"}).status_code == 200

    changes = h.worm.config_changes()
    assert len(changes) == 2
    assert "jev-1.13.0" in json.dumps(changes[1]["record"], default=str)
    assert "jev-1.14" in json.dumps(changes[1]["record"], default=str)


def test_config_audit_never_carries_the_key_value(h: Harness) -> None:
    assert _put_key(h).status_code == 200
    assert _put(h, {"enabled": True}).status_code == 200
    assert SENTINEL not in json.dumps(h.worm.records(), default=str)
