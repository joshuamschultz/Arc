"""RED — the always-on MCP door, mounted into the arcui Starlette server.

The MCP door (SPEC-082) is assembled per-agent by ``arcagent.build_mcp_door`` but
arcagent is headless and never binds a port. Today the door only serves through a
separate CLI process. Goal: make it always-on on the fleet by MOUNTING it into the
arcui app (``arcui.server.create_app``) so it comes up with ``arc.service`` — no
second process.

Intended API (what GREEN must satisfy):

- ``create_app`` adds a ``Mount("/mcp", app=<door sub-app>)`` to its routes.
- For ``POST /mcp/{agent_did}`` the sub-app resolves the LIVE started agent from
  ``app.state.embedded_agent_cache`` (an ``EmbeddedAgentCache``;
  ``.get(agent_did) -> ArcAgent | None``), reading it via ``scope["app"].state``.
- It builds the door via the facade ``arcagent.build_mcp_door(agent)`` (returns a
  ``BuiltDoor`` with ``.http_app``, an ``HttpDoor`` — a path-agnostic ASGI
  callable — or raises ``ValueError`` when ``[modules.mcp_server]`` is disabled).
- It delegates the RAW ASGI request to ``built.http_app`` (which only cares about
  method/body/tls, not the path).
- Unknown agent_did → 404. Disabled door (``ValueError``) → 404.
- ``/mcp/...`` is NOT behind the viewer/operator token auth — ``AuthMiddleware``
  skips non-``/api/`` paths; the door authenticates via its own signed envelope.

These tests fail RED today because the mount does not exist: a POST to
``/mcp/{did}`` currently matches only the SPA catch-all (GET/HEAD), so it answers
405, never 200/404 through the door.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    ModuleEntry,
    TelemetryConfig,
)
from arcstore.backends.memory import FakeBackend
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from arcui.auth import AuthConfig
from arcui.server import create_app


def _app(**kwargs: Any) -> Any:
    """An arcui app over an in-memory backend, with real (non-empty) auth tokens.

    Real tokens matter: if ``/mcp`` were mistakenly placed behind ``AuthMiddleware``,
    a token-less request would 401. The exemption test relies on that being a real
    401 surface, not a disabled one.
    """
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = create_app(arcstore_backend=FakeBackend(), auth_config=auth, **kwargs)
    app.state.auth_config = auth
    return app


class _FakeAgentCache:
    """Mirrors the ``EmbeddedAgentCache.get(agent_did) -> ArcAgent | None`` contract."""

    def __init__(self, agents: dict[str, Any] | None = None) -> None:
        self._agents = dict(agents or {})

    def get(self, agent_did: str) -> Any | None:
        return self._agents.get(agent_did)


async def _start_agent(tmp_path: Path, *, door_enabled: bool) -> ArcAgent:
    """Build and start a real minimal ArcAgent with the MCP door on or off."""
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    modules = (
        {"mcp_server": ModuleEntry(enabled=True, config={"expose": ["*"]})}
        if door_enabled
        else {}
    )
    config = ArcAgentConfig(
        agent=AgentConfig(
            name="door", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10000),
        modules=modules,
    )
    agent = ArcAgent(config=config)
    await agent.startup()
    return agent


def _tools_list_message() -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


async def _start_federal_door_agent(tmp_path: Path) -> ArcAgent:
    """A started door-enabled agent presenting as FEDERAL tier with a bounded expose.

    A real federal agent cannot start in a test env — the federal validator forces
    ``require_fips=True`` and there is no CMVP-validated OpenSSL provider here. So
    the agent starts at the default tier with a bounded (non-``*``) exposure the
    federal allowlist accepts, then the one attribute the door reads for its mTLS
    decision — ``security.tier`` — is set to ``federal``. This exercises the mount's
    delegation without standing up FIPS.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    config = ArcAgentConfig(
        agent=AgentConfig(
            name="door", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10000),
        # Bounded exposure: federal refuses expose=["*"], so name a real tool verb.
        modules={"mcp_server": ModuleEntry(enabled=True, config={"expose": ["read"]})},
    )
    agent = ArcAgent(config=config)
    await agent.startup()
    agent._config.security.tier = "federal"
    return agent


async def _post(app: Any, path: str, **kwargs: Any) -> httpx.Response:
    """POST through the FULL arcui app (routing + AuthMiddleware); no lifespan."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ui") as client:
        return await client.post(path, **kwargs)


async def _get(app: Any, path: str, **kwargs: Any) -> httpx.Response:
    """GET through the FULL arcui app (routing + AuthMiddleware); no lifespan."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ui") as client:
        return await client.get(path, **kwargs)


@asynccontextmanager
async def _sdk_session(app: Any, agent_did: str) -> AsyncIterator[ClientSession]:
    """A REAL initialized ``ClientSession`` against ``/mcp/{did}`` through the full app.

    Routes the SDK client's httpx traffic through ``ASGITransport`` (no socket) so the
    genuine MCP handshake runs against the mounted door exactly as an external client
    would reach it — the ``/mcp`` path is auth-exempt, so no token is presented.
    """

    def factory(
        headers: dict[str, str] | None = None,
        timeout: Any = None,
        auth: Any = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://ui",
            headers=headers,
            timeout=timeout,
        )

    async with streamablehttp_client(
        f"http://ui/mcp/{agent_did}", timeout=5.0, httpx_client_factory=factory
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


@pytest.mark.asyncio
async def test_post_tools_list_to_mounted_door_returns_the_agents_catalog(
    tmp_path: Path,
) -> None:
    """A started, door-enabled agent in the cache serves its tool catalog at /mcp/{did}."""
    agent = await _start_agent(tmp_path, door_enabled=True)
    try:
        app = _app()
        app.state.embedded_agent_cache = _FakeAgentCache({agent.did: agent})

        async with _sdk_session(app, agent.did) as session:
            listed = await session.list_tools()

        tools = listed.tools
        assert tools, "the mounted door served an empty tool catalog"
        for tool in tools:
            assert tool.name, "a served tool descriptor has no name"
            assert tool.inputSchema is not None, f"{tool.name} served without an inputSchema"
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_post_to_agent_with_disabled_door_is_404(tmp_path: Path) -> None:
    """An agent whose [modules.mcp_server] is disabled → 404 (build_mcp_door raises)."""
    agent = await _start_agent(tmp_path, door_enabled=False)
    try:
        app = _app()
        app.state.embedded_agent_cache = _FakeAgentCache({agent.did: agent})

        resp = await _post(app, f"/mcp/{agent.did}", json=_tools_list_message())

        assert resp.status_code == 404, resp.text
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_post_to_unknown_agent_did_is_404() -> None:
    """A DID not present in the embedded agent cache → 404."""
    app = _app()
    app.state.embedded_agent_cache = _FakeAgentCache({})

    resp = await _post(
        app, "/mcp/did:arc:testorg:executor/deadbeef", json=_tools_list_message()
    )

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_mcp_path_is_not_behind_viewer_operator_auth(tmp_path: Path) -> None:
    """An unauthenticated /mcp request reaches the door (200), never a 401.

    ``AuthMiddleware`` skips non-``/api/`` paths; the door authenticates via its own
    signed envelope, not the dashboard's viewer/operator tokens. The control below
    proves auth IS active on this app — so the /mcp non-401 is an exemption, not a
    disabled middleware.
    """
    agent = await _start_agent(tmp_path, door_enabled=True)
    try:
        app = _app()
        app.state.embedded_agent_cache = _FakeAgentCache({agent.did: agent})

        # Control: a real /api/ path with no token is a genuine 401 on this app.
        control = await _post(app, "/api/agents", json={})
        assert control.status_code == 401, "auth is not active — exemption test is moot"

        # The door path, same app, no Authorization header: a real SDK handshake +
        # listing SUCCEEDS, which is only possible if /mcp is exempt from auth (an
        # auth-gated path would 401 the handshake before the door ever ran).
        async with _sdk_session(app, agent.did) as session:
            listed = await session.list_tools()

        assert listed.tools, "the auth-exempt /mcp door served no catalog"
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_federal_door_refuses_a_certless_request_through_the_mount(
    tmp_path: Path,
) -> None:
    """A federal agent's door still 403s a certless POST THROUGH the mount.

    The mount delegates the RAW ASGI request untouched, so the door sees the same
    tier and (absent) TLS extension it would serving standalone — proving the mount
    preserves the enterprise/federal mTLS gate rather than pre-answering the request.
    """
    agent = await _start_federal_door_agent(tmp_path)
    try:
        app = _app()
        app.state.embedded_agent_cache = _FakeAgentCache({agent.did: agent})

        resp = await _post(app, f"/mcp/{agent.did}", json=_tools_list_message())

        assert resp.status_code == 403, resp.text
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_get_reaches_the_door_and_gets_its_own_405(tmp_path: Path) -> None:
    """GET /mcp/{did} reaches the door (405 — POST only), not the SPA HTML fallback.

    The door answers non-POST with its own 405. Seeing 405 (not a 200 HTML shell)
    proves the ``/mcp`` mount owns the path ahead of the SPA catch-all.
    """
    agent = await _start_agent(tmp_path, door_enabled=True)
    try:
        app = _app()
        app.state.embedded_agent_cache = _FakeAgentCache({agent.did: agent})

        resp = await _get(app, f"/mcp/{agent.did}")

        assert resp.status_code == 405, resp.text
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_missing_embedded_agent_cache_is_404_not_500() -> None:
    """When the embedded gateway is unwired, ``app.state`` has no cache → 404, not 500.

    The guard keeps an air-gapped or read-only dashboard (no embedded agents) from
    500-ing on a door request; the mount degrades to a clean 404.
    """
    app = _app()  # note: embedded_agent_cache is never set on app.state

    resp = await _post(
        app, "/mcp/did:arc:testorg:executor/deadbeef", json=_tools_list_message()
    )

    assert resp.status_code == 404, resp.text
