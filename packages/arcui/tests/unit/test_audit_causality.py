"""Item 20 — arcui attributes audit to the real initiator and shows a verified ledger.

Before: every UI act was recorded as the OPERATOR (the signing key's DID), the
Security screen read 0/100 VERIFIED (Observe never had the key), and the
``filter`` tabs did nothing. These tests drive the real ``AuthMiddleware``, the
real operator-signed arcui chain, and the real Observe ingest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust import OperatorKey, causal, default_operator_key_path
from arctrust.audit import AuditEvent, WormSink, signer_fingerprint
from arctrust.keypair import generate_keypair
from arctrust.signer import InProcessSigner
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from arcui.audit import MutationWormWriter, build_mutation_worm_writer, emit_mutation_audit
from arcui.auth import AuthConfig, AuthMiddleware, SessionTracker
from arcui.observe import Observe
from arcui.routes.keys import routes as keys_routes

_AUTH = {"viewer_token": "viewer", "operator_token": "operator"}


def _chain_events(data_dir: Path) -> list[dict[str, Any]]:
    chain = data_dir / "worm" / "audit-chain-arcui.jsonl"
    return [json.loads(line) for line in chain.read_text().splitlines() if line]


def _mutate(request: Request) -> JSONResponse:
    emit_mutation_audit(request, target="task:1", operation="task.cancel", outcome="applied")
    return JSONResponse({"ok": True})


@pytest.fixture
def ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, MutationWormWriter]:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    writer = build_mutation_worm_writer(tmp_path / "data")
    assert writer is not None
    auth = AuthConfig(_AUTH)
    app = Starlette(routes=[*keys_routes, Route("/api/x/mutate", _mutate, methods=["POST"])])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.session_tracker = SessionTracker()
    app.state.audit_worm = writer
    return TestClient(app), writer


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestUiAttribution:
    def test_a_page_view_is_attributed_to_the_ui_session_not_the_operator(
        self, ui: tuple[TestClient, MutationWormWriter], tmp_path: Path
    ) -> None:
        client, writer = ui
        assert client.get("/api/keys", headers=_bearer("viewer")).status_code == 200
        writer.sink.close()

        (record,) = [
            r
            for r in _chain_events(tmp_path / "data")
            if r["event"]["action"] == "provider_key.list"
        ]
        event = record["event"]
        assert event["actor_did"] != writer.operator_did
        assert event["actor_did"].startswith("did:arc:ui:session:")
        assert event["causal"]["initiator"] == "ui_session"
        assert event["causal"]["initiator_id"] == event["actor_did"]
        # The operator key still SIGNS the record; that is recorded separately.
        assert record["signer"] == signer_fingerprint(writer.sink.public_key)

    def test_a_mutation_records_the_session_that_made_it(
        self, ui: tuple[TestClient, MutationWormWriter], tmp_path: Path
    ) -> None:
        client, writer = ui
        assert client.post("/api/x/mutate", headers=_bearer("operator")).status_code == 200
        writer.sink.close()
        (record,) = [
            r for r in _chain_events(tmp_path / "data") if r["event"]["action"] == "task.cancel"
        ]
        assert record["event"]["actor_did"] != writer.operator_did
        assert record["event"]["causal"]["initiator"] == "ui_session"

    def test_two_sessions_are_two_actors(
        self, ui: tuple[TestClient, MutationWormWriter], tmp_path: Path
    ) -> None:
        client, writer = ui
        client.get("/api/keys", headers=_bearer("viewer"))
        client.get("/api/keys", headers=_bearer("operator"))
        writer.sink.close()
        actors = {
            r["event"]["actor_did"]
            for r in _chain_events(tmp_path / "data")
            if r["event"]["action"] == "provider_key.list"
        }
        assert len(actors) == 2

    def test_a_forged_causal_header_is_ignored(
        self, ui: tuple[TestClient, MutationWormWriter], tmp_path: Path
    ) -> None:
        client, writer = ui
        forged = {"initiator": "operator", "initiator_id": "did:arc:operator:root"}
        headers = {**_bearer("viewer"), "X-Arc-Causal": json.dumps(forged)}
        client.get("/api/keys", headers=headers)
        writer.sink.close()
        events = [r["event"] for r in _chain_events(tmp_path / "data")]
        assert all(e["causal"]["initiator"] == "ui_session" for e in events)
        assert all(e["actor_did"] != "did:arc:operator:root" for e in events)

    def test_the_request_binding_never_outlives_the_request(
        self, ui: tuple[TestClient, MutationWormWriter]
    ) -> None:
        client, _ = ui
        client.get("/api/keys", headers=_bearer("viewer"))
        assert causal.current() is None


def _signed_chain(worm_dir: Path, outcomes: list[str], name: str) -> bytes:
    kp = generate_keypair()
    sink = WormSink(worm_dir / name, InProcessSigner(kp.private_key))
    for i, outcome in enumerate(outcomes):
        sink.write(
            AuditEvent(
                actor_did="did:arc:t:a/1",
                action="policy.evaluate",
                target=f"t{i}",
                outcome=outcome,
            )
        )
    sink.close()
    return kp.public_key


class TestObserveLedger:
    async def test_the_verified_count_goes_up_once_observe_has_the_key(
        self, tmp_path: Path
    ) -> None:
        worm = tmp_path / "worm"
        worm.mkdir()
        key = _signed_chain(worm, ["allow", "deny", "allow"], "audit-chain-olivia.jsonl")

        blind = Observe(data_dir=tmp_path, backend=FakeBackend())
        await blind.refresh()
        assert (await blind.audit_totals())["verified"] == 0

        observe = Observe(data_dir=tmp_path, backend=FakeBackend(), worm_public_key=key)
        await observe.refresh()
        assert await observe.audit_totals() == {"total": 3, "verified": 3, "broken": 0}

    async def test_filters_select_denials_and_control_actions(self, tmp_path: Path) -> None:
        worm = tmp_path / "worm"
        worm.mkdir()
        key = _signed_chain(worm, ["allow", "deny"], "audit-chain-olivia.jsonl")
        observe = Observe(data_dir=tmp_path, backend=FakeBackend(), worm_public_key=key)
        await observe.refresh()

        denials = await observe.audit(category="deny")
        assert [e["outcome"] for e in denials] == ["deny"]
        assert await observe.audit(category="control") == []
        assert len(await observe.audit()) == 2

    async def test_events_expose_the_causal_fields(self, tmp_path: Path) -> None:
        worm = tmp_path / "worm"
        worm.mkdir()
        kp = generate_keypair()
        sink = WormSink(worm / "audit-chain-olivia.jsonl", InProcessSigner(kp.private_key))
        with (
            causal.bind(causal.root("agent", "did:arc:t:a/1")),
            causal.refine(run_id="r1", tool_call_id="c1"),
        ):
            sink.write(
                AuditEvent(
                    actor_did="did:arc:t:a/1",
                    action="policy.evaluate",
                    target="t",
                    outcome="allow",
                )
            )
        sink.close()
        observe = Observe(data_dir=tmp_path, backend=FakeBackend(), worm_public_key=kp.public_key)
        await observe.refresh()
        (event,) = await observe.audit()
        assert event["initiator"] == "agent"
        assert event["run_id"] == "r1"
        assert event["tool_call_id"] == "c1"
        assert event["signer"] == signer_fingerprint(kp.public_key)
        assert event["verified"] is True
