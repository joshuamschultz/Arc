"""SPEC-017 R-055 — Network egress proxy for dynamic tools.

Deny-by-default outbound HTTP. Dynamic tools reach the network only
through ``ToolContext.http``; any endpoint not on the per-tool
allowlist is rejected, and every request is audit-logged.

This test suite validates the proxy's allow/deny decision and audit
emission without making real HTTP calls — the proxy delegates to an
``httpx``-shaped ``send_fn`` so we inject a test stub.
"""

from __future__ import annotations

import pytest


class _CapturedRequest:
    def __init__(self, url: str, method: str = "GET") -> None:
        self.url = url
        self.method = method


class _Response:
    def __init__(self, status: int = 200, body: str = "ok") -> None:
        self.status_code = status
        self.text = body


class TestAllowlistEnforcement:
    async def test_allowlisted_endpoint_passes_through(self) -> None:
        from arcagent.tools._egress import EgressProxy

        sent: list[_CapturedRequest] = []

        async def send_fn(url: str, method: str, **_: object) -> _Response:
            sent.append(_CapturedRequest(url, method))
            return _Response(200)

        proxy = EgressProxy(
            allowlist={"https://api.example.com"},
            send_fn=send_fn,
        )
        response = await proxy.request("https://api.example.com/v1/ping", method="GET")
        assert response.status_code == 200
        assert len(sent) == 1

    async def test_non_allowlisted_endpoint_rejected(self) -> None:
        from arcagent.tools._egress import EgressDenied, EgressProxy

        async def send_fn(_url: str, _method: str, **_: object) -> _Response:
            return _Response()

        proxy = EgressProxy(
            allowlist={"https://api.example.com"},
            send_fn=send_fn,
        )
        with pytest.raises(EgressDenied) as exc_info:
            await proxy.request("https://evil.example.com/exfil", method="POST")
        assert "evil.example.com" in str(exc_info.value)

    async def test_empty_allowlist_denies_everything(self) -> None:
        from arcagent.tools._egress import EgressDenied, EgressProxy

        async def send_fn(_url: str, _method: str, **_: object) -> _Response:
            return _Response()

        proxy = EgressProxy(allowlist=set(), send_fn=send_fn)
        with pytest.raises(EgressDenied):
            await proxy.request("https://anywhere.example.com/x", method="GET")


class TestDynamicToolEgressWiring:
    """A sandboxed dynamic tool reaches the network ONLY through the proxy.

    Proves ``_runtime.egress()`` has a real caller: the bare name ``egress`` is
    injected into the restricted sandbox namespace, so agent-authored source can
    route outbound HTTP through the allowlist-gated, audited proxy — and nowhere
    else.
    """

    async def test_dynamic_tool_egresses_through_proxy(self) -> None:
        from arcagent.builtins.capabilities import _runtime
        from arcagent.tools._dynamic_loader import DynamicToolLoader
        from arcagent.tools._egress import EgressProxy

        _runtime.reset()
        sent: list[str] = []

        async def send_fn(url: str, _method: str, **_: object) -> _Response:
            sent.append(url)
            return _Response(200)

        proxy = EgressProxy(allowlist={"https://api.example.com"}, send_fn=send_fn)
        _runtime.configure(workspace=__import__("pathlib").Path("."), egress_proxy=proxy)

        source = (
            "from arcagent.tools._decorator import tool\n"
            "@tool(name='fetcher', description='fetch', capability_tags=['network_egress'])\n"
            "async def fetcher() -> str:\n"
            "    resp = await egress().request('https://api.example.com/data')\n"
            "    return f'status={resp.status_code}'\n"
        )
        loader = DynamicToolLoader()
        registered = loader.load(source, name="fetcher")
        result = await registered.execute()
        assert result == "status=200"
        assert sent == ["https://api.example.com/data"]
        _runtime.reset()


class TestOriginMatching:
    """Allowlist matches by origin (scheme+host+port), not full URL."""

    async def test_different_path_same_origin_allowed(self) -> None:
        from arcagent.tools._egress import EgressProxy

        async def send_fn(_url: str, _method: str, **_: object) -> _Response:
            return _Response()

        proxy = EgressProxy(
            allowlist={"https://api.example.com"},
            send_fn=send_fn,
        )
        response = await proxy.request("https://api.example.com/v1/foo", method="GET")
        response_2 = await proxy.request("https://api.example.com/v2/bar?x=1", method="POST")
        assert response.status_code == 200
        assert response_2.status_code == 200

    async def test_different_port_rejected(self) -> None:
        from arcagent.tools._egress import EgressDenied, EgressProxy

        async def send_fn(_url: str, _method: str, **_: object) -> _Response:
            return _Response()

        proxy = EgressProxy(
            allowlist={"https://api.example.com"},
            send_fn=send_fn,
        )
        with pytest.raises(EgressDenied):
            await proxy.request("https://api.example.com:8443/v1/foo", method="GET")


class TestAuditEmission:
    """Every request — allow or deny — is audit-logged."""

    async def test_allowed_request_audits(self) -> None:
        from arcagent.tools._egress import EgressProxy

        async def send_fn(_url: str, _method: str, **_: object) -> _Response:
            return _Response(200)

        events: list[tuple[str, dict[str, object]]] = []
        proxy = EgressProxy(
            allowlist={"https://api.example.com"},
            send_fn=send_fn,
            audit_sink=lambda e, d: events.append((e, d)),
        )
        await proxy.request("https://api.example.com/x", method="GET")
        assert any(e[0] == "egress.allowed" for e in events)

    async def test_denied_request_audits(self) -> None:
        from arcagent.tools._egress import EgressDenied, EgressProxy

        async def send_fn(_url: str, _method: str, **_: object) -> _Response:
            return _Response()

        events: list[tuple[str, dict[str, object]]] = []
        proxy = EgressProxy(
            allowlist={"https://api.example.com"},
            send_fn=send_fn,
            audit_sink=lambda e, d: events.append((e, d)),
        )
        with pytest.raises(EgressDenied):
            await proxy.request("https://evil.example.com/x", method="POST")
        deny_events = [e for e in events if e[0] == "egress.denied"]
        assert len(deny_events) == 1
        assert deny_events[0][1]["url"] == "https://evil.example.com/x"
