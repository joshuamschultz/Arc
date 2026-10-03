"""The Knowledge panel reads a shared connection store as the agent does (P18-4).

A connection several agents read is synced into one store no agent owns, so the
agent's own workspace holds none of its documents. The panel asks the running
agent's service, which applies the same subscription check the agent's turns do;
anything else still reads the agent's own store.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.knowledge import routes

_DID = "did:arc:olivia"


class _Hit:
    def __init__(self, text: str) -> None:
        self.text = text

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        del mode
        return {"text": self.text}


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def shared_documents(
        self, source_id: str, *, caller_did: str, query: str | None = None, limit: int = 50
    ) -> list[_Hit] | None:
        del limit
        self.calls.append((source_id, caller_did, query))
        return [_Hit("the billing portal ships in August")] if source_id == "shared-pool" else None


class _Registry:
    def __init__(self, service: _Service) -> None:
        self._entry = SimpleNamespace(instance=SimpleNamespace(service=service))

    async def get_capability(self, name: str) -> Any:
        return self._entry if name == "connected_data" else None


def _client(tmp_path: Any) -> tuple[TestClient, _Service]:
    service = _Service()
    app = Starlette(routes=routes)
    app.add_middleware(
        AuthMiddleware,
        auth_config=AuthConfig({"viewer_token": "viewer", "operator_token": "operator"}),
    )
    roster = SimpleNamespace(agent_id="olivia", did=_DID, workspace_path=str(tmp_path))
    app.state.roster_provider = lambda: [roster]
    app.state.embedded_agent_cache = {
        _DID: SimpleNamespace(_capability_registry=_Registry(service))
    }
    return TestClient(app), service


def test_a_shared_pool_is_read_through_the_running_agents_subscription(tmp_path: Any) -> None:
    client, service = _client(tmp_path)

    response = client.get(
        "/api/agents/olivia/knowledge/documents?source=shared-pool&q=billing",
        headers={"Authorization": "Bearer viewer"},
    )

    assert response.status_code == 200
    assert response.json() == {"items": [{"text": "the billing portal ships in August"}]}
    assert service.calls == [("shared-pool", _DID, "billing")]


def test_any_other_pool_still_reads_the_agents_own_store(tmp_path: Any) -> None:
    client, service = _client(tmp_path)

    response = client.get(
        "/api/agents/olivia/knowledge/documents?source=own-pool&q=billing",
        headers={"Authorization": "Bearer viewer"},
    )

    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert service.calls == [("own-pool", _DID, "billing")]
