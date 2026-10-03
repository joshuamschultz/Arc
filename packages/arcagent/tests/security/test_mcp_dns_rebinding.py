"""P12 abuse battery — DNS rebinding against an operator-added HTTP MCP server.

An operator-added URL is SSRF-checked when it is added, but a hostname can be re-pointed
afterwards: first answer public (passes the add-time check), later answer link-local or
metadata (reaches the cloud credential endpoint). The connect path must therefore resolve
the host itself, judge EVERY address it got, connect to the address it judged, keep the
original host for TLS and the Host header, and do all of that again on every connect.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from arcagent.core.tier import Tier
from arcagent.extension.egress_guard import EgressPolicy, EgressRefusedError
from arcagent.extension.pinned_transport import PinnedResolverTransport, pinned_client_factory

_HOST = "mcp.example.com"
_PUBLIC = "93.184.216.34"
_METADATA = "169.254.169.254"


class _Rebinder:
    """A resolver whose answers change on each lookup, like a hostile DNS server."""

    def __init__(self, *answers: list[str]) -> None:
        self._answers = list(answers)
        self.lookups = 0

    def __call__(self, host: str) -> list[str]:
        answer = self._answers[min(self.lookups, len(self._answers) - 1)]
        self.lookups += 1
        return answer


def _policy(resolver: Callable[[str], list[str]], tier: Tier = Tier.ENTERPRISE) -> EgressPolicy:
    return EgressPolicy(tier=tier, resolver=resolver)


class _Inner(httpx.AsyncBaseTransport):
    """Stands in for the network: records what would have been connected to."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True})


def _client(
    resolver: Callable[[str], list[str]],
    tier: Tier = Tier.ENTERPRISE,
    refusals: list[tuple[str, str, str]] | None = None,
) -> tuple[httpx.AsyncClient, _Inner]:
    inner = _Inner()
    sink = None
    if refusals is not None:

        def sink(host: str, address: str, reason: str) -> None:
            refusals.append((host, address, reason))

    transport = PinnedResolverTransport(_policy(resolver, tier), inner=inner, on_refused=sink)
    return httpx.AsyncClient(transport=transport), inner


async def test_connects_to_the_validated_address_and_keeps_host_and_sni() -> None:
    client, inner = _client(_Rebinder([_PUBLIC]))
    async with client:
        await client.post(f"https://{_HOST}/mcp", json={})

    (sent,) = inner.requests
    assert sent.url.host == _PUBLIC  # the socket goes to the address that was judged
    assert sent.headers["host"] == _HOST  # the server still sees its own name
    assert sent.extensions["sni_hostname"] == _HOST  # and TLS verifies the name, not the IP


async def test_rebind_to_metadata_on_the_second_connect_is_refused_and_audited() -> None:
    refusals: list[tuple[str, str, str]] = []
    client, inner = _client(_Rebinder([_PUBLIC], [_METADATA]), refusals=refusals)
    async with client:
        await client.post(f"https://{_HOST}/mcp", json={})
        with pytest.raises(EgressRefusedError):
            await client.post(f"https://{_HOST}/mcp", json={})

    assert len(inner.requests) == 1  # the metadata address was never connected to
    ((host, address, _reason),) = refusals
    assert (host, address) == (_HOST, _METADATA)


async def test_one_bad_address_among_good_ones_refuses_the_whole_lookup() -> None:
    client, inner = _client(_Rebinder([_PUBLIC, _METADATA]))
    async with client:
        with pytest.raises(EgressRefusedError):
            await client.post(f"https://{_HOST}/mcp", json={})
    assert inner.requests == []


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "169.254.10.10",
        "fd00:ec2::254",
        "::ffff:169.254.169.254",
        "100.100.100.200",
        "fe80::1",
        "0.0.0.0",  # noqa: S104 - the address under test, not a bind
        "224.0.0.1",
        "ff02::1",
    ],
)
@pytest.mark.parametrize("tier", [Tier.PERSONAL, Tier.ENTERPRISE, Tier.FEDERAL])
async def test_never_reachable_addresses_are_refused_at_every_tier(
    address: str, tier: Tier
) -> None:
    client, inner = _client(_Rebinder([address]), tier=tier)
    async with client:
        with pytest.raises(EgressRefusedError):
            await client.get(f"https://{_HOST}/mcp")
    assert inner.requests == []


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "10.1.2.3", "192.168.0.9", "fd12::1"])
async def test_loopback_and_private_are_refused_above_personal_tier(address: str) -> None:
    client, inner = _client(_Rebinder([address]), tier=Tier.ENTERPRISE)
    async with client:
        with pytest.raises(EgressRefusedError):
            await client.get(f"https://{_HOST}/mcp")
    assert inner.requests == []


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3"])
async def test_personal_tier_may_reach_its_own_machine_and_lan(address: str) -> None:
    client, inner = _client(_Rebinder([address]), tier=Tier.PERSONAL)
    async with client:
        await client.get(f"https://{_HOST}/mcp")
    assert len(inner.requests) == 1


async def test_an_allowlisted_private_range_is_reachable_above_personal_tier() -> None:
    import ipaddress

    inner = _Inner()
    policy = EgressPolicy(
        tier=Tier.ENTERPRISE,
        private_allowlist=(ipaddress.ip_network("10.1.0.0/16"),),
        resolver=_Rebinder(["10.1.2.3"]),
    )
    async with httpx.AsyncClient(transport=PinnedResolverTransport(policy, inner=inner)) as client:
        await client.get(f"https://{_HOST}/mcp")
    assert inner.requests[0].url.host == "10.1.2.3"


async def test_a_redirect_to_a_rebinding_host_is_judged_on_its_own_lookup() -> None:
    hops: list[str] = []

    class _Redirecting(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            hops.append(request.url.host)
            if len(hops) == 1:
                return httpx.Response(302, headers={"location": "https://evil.example/mcp"})
            return httpx.Response(200)

    answers = {_HOST: [_PUBLIC], "evil.example": [_METADATA]}
    policy = _policy(lambda host: answers[host])
    transport = PinnedResolverTransport(policy, inner=_Redirecting())
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(EgressRefusedError):
            await client.get(f"https://{_HOST}/mcp")
    assert hops == [_PUBLIC]  # the second hop never left


async def test_a_literal_metadata_address_is_refused_without_a_lookup() -> None:
    resolver = _Rebinder([_PUBLIC])
    client, inner = _client(resolver)
    async with client:
        with pytest.raises(EgressRefusedError):
            await client.get(f"http://{_METADATA}/latest/meta-data")
    assert inner.requests == [] and resolver.lookups == 0


async def test_a_failed_lookup_is_refused_not_passed_through() -> None:
    def broken(host: str) -> list[str]:
        raise OSError("no such host")

    client, inner = _client(broken)
    async with client:
        with pytest.raises(EgressRefusedError):
            await client.get(f"https://{_HOST}/mcp")
    assert inner.requests == []


def test_the_client_factory_builds_a_pinned_client_with_the_callers_headers() -> None:
    factory = pinned_client_factory(_policy(_Rebinder([_PUBLIC])), None)
    client = factory(headers={"x-api-key": "k"}, timeout=httpx.Timeout(5.0), auth=None)
    assert isinstance(client._transport, PinnedResolverTransport)
    assert client.headers["x-api-key"] == "k"
    assert client.follow_redirects is True


async def test_for_http_hands_the_sdk_a_pinned_client_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arcagent.extension import mcp_attachment

    captured: dict[str, Any] = {}

    class _StubHttp:
        def __call__(self, url: str, **kwargs: Any) -> _StubHttp:
            captured.update(kwargs)
            return self

        async def __aenter__(self) -> tuple[str, str, str]:
            return ("r", "w", "x")

        async def __aexit__(self, *exc: object) -> bool:
            return False

    class _StubSession:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _StubSession:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def initialize(self) -> None:
            return None

    monkeypatch.setattr(mcp_attachment, "streamablehttp_client", _StubHttp())
    monkeypatch.setattr(mcp_attachment, "ClientSession", _StubSession)

    client = mcp_attachment.SdkMcpClient.for_http(
        url="https://vendor/mcp", egress=_policy(_Rebinder([_PUBLIC]))
    )
    async with client._session_factory():
        pass

    built = captured["httpx_client_factory"](headers=None, timeout=None, auth=None)
    assert isinstance(built._transport, PinnedResolverTransport)


async def test_for_http_without_a_policy_is_pinned_at_the_strict_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that forgets the policy gets the safe one, never an unpinned client."""
    from arcagent.extension import mcp_attachment

    captured: dict[str, Any] = {}

    class _StubHttp:
        def __call__(self, url: str, **kwargs: Any) -> _StubHttp:
            captured.update(kwargs)
            raise RuntimeError("stop after capture")

    monkeypatch.setattr(mcp_attachment, "streamablehttp_client", _StubHttp())
    client = mcp_attachment.SdkMcpClient.for_http(url="https://vendor/mcp")
    with pytest.raises(RuntimeError):
        async with client._session_factory():
            pass

    transport = captured["httpx_client_factory"](headers=None, timeout=None, auth=None)._transport
    assert transport._policy.tier is Tier.ENTERPRISE


_HTTP_MANIFEST = """
[extension]
name = "hosted_mcp"
version = "1.0.0"
attachment = "mcp"

[tools]
allow = ["remote_read"]

[[tools.declared]]
name = "remote_read"
description = "Read a remote record."
classification = "read_only"

[config.mcp]
transport = "http"
url = "https://mcp.example.com/endpoint"

[config.mcp.tools.remote_read]
classification = "read_only"
"""


def test_build_attachment_binds_the_deployment_tier_and_the_audit_sink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from arcagent.extension.manifest import load_manifest
    from arcagent.extension.mcp_attachment import SdkMcpClient
    from arcagent.modules.connectors.attachments import build_attachment

    seen: dict[str, Any] = {}

    def _for_http(**kwargs: Any) -> str:
        seen.update(kwargs)
        return "client"

    monkeypatch.setattr(SdkMcpClient, "for_http", staticmethod(_for_http))
    monkeypatch.setattr(
        "arcagent.modules.connectors.attachments._namespaced", lambda client, _ns: client
    )
    monkeypatch.setattr(
        "arcagent.modules.connectors.attachments._with_source_adapter",
        lambda _manifest, _bundle, client: client,
    )

    def sink(host: str, address: str, reason: str) -> None:
        return None

    build_attachment(
        load_manifest(_HTTP_MANIFEST, tier=Tier.ENTERPRISE),
        tmp_path,
        {},
        tier=Tier.ENTERPRISE,
        egress_audit=sink,
    )

    assert seen["egress"].tier is Tier.ENTERPRISE
    assert seen["on_egress_refused"] is sink


def test_a_refused_connect_becomes_a_connector_audit_event() -> None:
    from types import SimpleNamespace

    from arcagent.modules.connectors.capabilities import _egress_audit

    events: list[Any] = []

    class _Sink:
        def write(self, event: Any) -> None:
            events.append(event)

    state = SimpleNamespace(
        identity=SimpleNamespace(did="did:arc:test:agent/one"), tier="enterprise"
    )
    _egress_audit(state, _Sink(), "acme")(_HOST, _METADATA, "link-local")  # type: ignore[arg-type]

    (event,) = events
    assert event.action == "connector.egress_denied" and event.outcome == "deny"
    assert event.extra["host"] == _HOST and event.extra["address"] == _METADATA
