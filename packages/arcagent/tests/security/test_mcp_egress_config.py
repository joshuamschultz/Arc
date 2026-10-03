"""Operator egress config: ``[tools.policy] egress_allow_cidrs`` and ``mcp_via_proxy``."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import httpx
import pytest

from arcagent.core.config import ToolConfig
from arcagent.core.tier import Tier
from arcagent.extension.egress_guard import (
    EgressPolicy,
    EgressRefusedError,
    parse_allow_cidrs,
)
from arcagent.extension.pinned_transport import PinnedResolverTransport

_HOST = "mcp.internal.example"


class _Inner(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200)


def test_cidrs_parse_to_networks() -> None:
    nets = parse_allow_cidrs(["10.0.0.0/8", "192.168.1.5"])
    assert ipaddress.ip_network("10.0.0.0/8") in nets
    assert ipaddress.ip_network("192.168.1.5/32") in nets


@pytest.mark.parametrize("bad", ["not-a-cidr", "10.0.0.1/99", "", "mcp.example.com"])
def test_a_malformed_cidr_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_allow_cidrs([bad])
    with pytest.raises(ValueError):
        ToolConfig(egress_allow_cidrs=[bad])


@pytest.mark.parametrize(
    "never", ["169.254.0.0/16", "169.254.169.254/32", "224.0.0.0/4", "0.0.0.0/32", "fe80::/10"]
)
def test_never_reachable_space_is_refused_even_when_listed(never: str) -> None:
    with pytest.raises(ValueError, match="never"):
        parse_allow_cidrs([never])


def test_a_range_that_merely_overlaps_blocked_space_is_refused() -> None:
    # 0.0.0.0/0 contains the metadata address; listing it must not unlock it.
    with pytest.raises(ValueError):
        parse_allow_cidrs(["0.0.0.0/0"])


async def test_a_listed_range_is_reachable_above_personal_tier() -> None:
    inner = _Inner()
    policy = EgressPolicy(
        tier=Tier.ENTERPRISE,
        private_allowlist=parse_allow_cidrs(["10.0.0.0/8"]),
        resolver=lambda _h: ["10.4.5.6"],
    )
    async with httpx.AsyncClient(transport=PinnedResolverTransport(policy, inner=inner)) as c:
        await c.get(f"https://{_HOST}/mcp")
    assert inner.requests[0].url.host == "10.4.5.6"


def test_the_policy_still_refuses_metadata_when_a_hand_built_allowlist_lists_it() -> None:
    policy = EgressPolicy(
        tier=Tier.ENTERPRISE,
        private_allowlist=(ipaddress.ip_network("169.254.0.0/16"),),
    )
    assert policy.refusal(ipaddress.ip_address("169.254.169.254")) is not None


def test_deployment_reads_cidrs_and_proxy_flag(tmp_path: Path, monkeypatch) -> None:
    from arcagent import connections

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "arcagent.toml").write_text(
        '[tools.policy]\negress_allow_cidrs = ["10.0.0.0/8"]\nmcp_via_proxy = true\n'
    )
    monkeypatch.setattr(connections, "config_file", lambda name, root: tmp_path / "config" / name)
    policy = connections.deployment_egress_policy(Tier.ENTERPRISE, tmp_path)
    assert policy.private_allowlist == (ipaddress.ip_network("10.0.0.0/8"),)
    assert policy.via_proxy is True


def test_deployment_with_a_bad_cidr_fails_closed(tmp_path: Path, monkeypatch) -> None:
    from arcagent import connections

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "arcagent.toml").write_text(
        '[tools.policy]\negress_allow_cidrs = ["bogus"]\n'
    )
    monkeypatch.setattr(connections, "config_file", lambda name, root: tmp_path / "config" / name)
    policy = connections.deployment_egress_policy(Tier.ENTERPRISE, tmp_path)
    assert policy.private_allowlist == ()  # a malformed list grants nothing


# --- proxy mode --------------------------------------------------------------


def _proxy_transport(
    policy: EgressPolicy, env: dict[str, str]
) -> tuple[PinnedResolverTransport, _Inner, _Inner]:
    direct, proxied = _Inner(), _Inner()
    transport = PinnedResolverTransport(
        policy, inner=direct, proxy=proxied, bypass=lambda host: host == "direct.example"
    )
    return transport, direct, proxied


async def test_via_proxy_forwards_the_name_unresolved() -> None:
    lookups: list[str] = []

    def resolver(host: str) -> list[str]:
        lookups.append(host)
        return ["93.184.216.34"]

    policy = EgressPolicy(tier=Tier.ENTERPRISE, via_proxy=True, resolver=resolver)
    transport, direct, proxied = _proxy_transport(policy, {})
    async with httpx.AsyncClient(transport=transport) as c:
        await c.get(f"https://{_HOST}/mcp")
    assert lookups == [] and direct.requests == []
    assert proxied.requests[0].url.host == _HOST  # the proxy resolves it, not us


async def test_via_proxy_still_refuses_a_literal_blocked_address() -> None:
    policy = EgressPolicy(tier=Tier.ENTERPRISE, via_proxy=True)
    transport, direct, proxied = _proxy_transport(policy, {})
    async with httpx.AsyncClient(transport=transport) as c:
        with pytest.raises(EgressRefusedError):
            await c.get("http://169.254.169.254/latest")
    assert proxied.requests == [] and direct.requests == []


async def test_no_proxy_hosts_take_the_pinned_direct_path() -> None:
    policy = EgressPolicy(
        tier=Tier.ENTERPRISE, via_proxy=True, resolver=lambda _h: ["93.184.216.34"]
    )
    transport, direct, proxied = _proxy_transport(policy, {})
    async with httpx.AsyncClient(transport=transport) as c:
        await c.get("https://direct.example/mcp")
    assert proxied.requests == []
    assert direct.requests[0].url.host == "93.184.216.34"


async def test_without_the_opt_in_the_proxy_is_never_used_above_personal() -> None:
    policy = EgressPolicy(
        tier=Tier.ENTERPRISE, via_proxy=False, resolver=lambda _h: ["93.184.216.34"]
    )
    transport, direct, proxied = _proxy_transport(policy, {})
    async with httpx.AsyncClient(transport=transport) as c:
        await c.get(f"https://{_HOST}/mcp")
    assert proxied.requests == []
    assert direct.requests[0].url.host == "93.184.216.34"


async def test_personal_tier_honors_the_proxy_without_the_flag() -> None:
    policy = EgressPolicy(tier=Tier.PERSONAL, resolver=lambda _h: ["93.184.216.34"])
    transport, direct, proxied = _proxy_transport(policy, {})
    async with httpx.AsyncClient(transport=transport) as c:
        await c.get(f"https://{_HOST}/mcp")
    assert proxied.requests and direct.requests == []


def test_the_factory_reads_https_proxy_and_no_proxy_from_the_environment(monkeypatch) -> None:
    from arcagent.extension.pinned_transport import proxy_from_environment

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("NO_PROXY", "direct.example,.local")
    proxy_url, bypass = proxy_from_environment()
    assert proxy_url == "http://proxy.corp:3128"
    assert bypass("direct.example") and bypass("a.local") and not bypass("mcp.example.com")
    monkeypatch.delenv("HTTPS_PROXY")
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    assert proxy_from_environment()[0] is None


def test_add_time_check_honors_the_allowlist_and_nothing_more() -> None:
    from arcagent.modules.connectors.mcp_bundle import McpServerSpec, validate_spec

    spec = McpServerSpec(name="internal", transport="http", url=f"https://{_HOST}/mcp", tools={})
    allow = parse_allow_cidrs(["10.0.0.0/8"])
    with pytest.raises(Exception, match="private"):
        validate_spec(spec, tier=Tier.ENTERPRISE, resolver=lambda _h: ["10.4.5.6"])
    validate_spec(
        spec, tier=Tier.ENTERPRISE, private_allowlist=allow, resolver=lambda _h: ["10.4.5.6"]
    )
    with pytest.raises(Exception, match="private"):
        validate_spec(
            spec, tier=Tier.ENTERPRISE, private_allowlist=allow, resolver=lambda _h: ["172.16.0.1"]
        )
