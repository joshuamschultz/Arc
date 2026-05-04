"""Lifespan integration test for SPEC-023 in-process gateway runtime.

Verifies that ``create_app(gateway_config=...)`` populates ``app.state``
with the components produced by ``arcgateway.bootstrap.build_for_embedded``
during Starlette lifespan startup, and tears them down cleanly on
shutdown.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcgateway.adapters.web import WebPlatformAdapter
from arcgateway.config import GatewayConfig
from arcgateway.session import SessionRouter
from starlette.testclient import TestClient

from arcui.server import create_app


@pytest.fixture
def team_root(tmp_path: Path) -> Path:
    root = tmp_path / "team"
    root.mkdir()
    return root


def test_lifespan_populates_app_state_when_gateway_config_present(
    team_root: Path,
) -> None:
    """With a non-None gateway_config and team_root, lifespan composes the runtime."""
    gateway_config = GatewayConfig.from_toml_str(
        """
[gateway]
agent_did = "did:arc:agent:default"

[platforms.web]
enabled = true
"""
    )
    app = create_app(team_root=team_root, gateway_config=gateway_config)
    with TestClient(app) as client:
        # Lifespan startup completes when TestClient enters its context.
        assert client.get("/api/health").status_code == 200
        assert isinstance(app.state.web_adapter, WebPlatformAdapter)
        assert isinstance(app.state.session_router, SessionRouter)
        assert app.state.embedded_gateway is not None


def test_lifespan_no_gateway_config_keeps_state_unset(team_root: Path) -> None:
    """Without gateway_config the embedded runtime is not built."""
    app = create_app(team_root=team_root)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert getattr(app.state, "web_adapter", None) is None
        assert getattr(app.state, "embedded_gateway", None) is None


def test_lifespan_disconnects_adapters_on_shutdown(team_root: Path) -> None:
    """Adapters disconnect cleanly when the Starlette context exits."""
    gateway_config = GatewayConfig.from_toml_str(
        """
[platforms.web]
enabled = true
"""
    )
    app = create_app(team_root=team_root, gateway_config=gateway_config)
    captured: dict[str, WebPlatformAdapter | None] = {"adapter": None}
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        captured["adapter"] = app.state.web_adapter
    # After context exit, the adapter has been disconnected — its socket map
    # is empty.
    adapter = captured["adapter"]
    assert adapter is not None
    assert adapter._socket_meta == {}
