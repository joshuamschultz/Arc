"""Which addresses an operator-added MCP server may be reached at.

One policy, two callers: the add-time URL check (``mcp_bundle``) and the connect-time
pinned transport (``pinned_transport``). Judging a name once at add time is not enough,
because the name can be re-pointed afterwards (DNS rebinding); the connect path therefore
re-applies this same policy to every address a lookup returns, every time it connects.

Always refused, at every tier: link-local, cloud-metadata, multicast, unspecified and
reserved space. Refused above personal tier: loopback, and private ranges unless the
operator listed the range. Personal tier may reach its own machine and LAN.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from arcagent.core.tier import Tier

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str], Sequence[str]]

METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(text)
    for text in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200", "192.0.0.192")
)


class EgressRefusedError(OSError):
    """A host resolved to an address an MCP server may not be reached at."""


def system_resolver(host: str) -> list[str]:
    """Every address the operating system resolver returns for ``host``."""
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]


def unmapped(address: IPAddress) -> IPAddress:
    """Unwrap ``::ffff:a.b.c.d`` so a mapped metadata address is judged as IPv4."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def parse_address(text: str) -> IPAddress:
    """An address from a resolver answer, with any IPv6 zone id dropped and v4-mapped unwrapped."""
    return unmapped(ipaddress.ip_address(text.split("%", 1)[0]))


def always_blocked(address: IPAddress) -> bool:
    """Link-local, metadata, multicast, unspecified or reserved: never reachable."""
    return (
        address in METADATA_ADDRESSES
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


_NEVER_REACHABLE: tuple[IPNetwork, ...] = (
    *(ipaddress.ip_network(address) for address in METADATA_ADDRESSES),
    *(
        ipaddress.ip_network(text)
        for text in (
            "169.254.0.0/16",
            "224.0.0.0/4",
            "240.0.0.0/4",
            "0.0.0.0/32",
            "fe80::/10",
            "ff00::/8",
            "::/8",
        )
    ),
)


def parse_allow_cidrs(entries: Sequence[str]) -> tuple[IPNetwork, ...]:
    """Operator-listed private ranges as networks, refusing any that touch never-reachable space.

    A bare address means a single host. A range that merely *contains* a metadata,
    link-local, multicast or reserved address (``0.0.0.0/0``, ``100.64.0.0/10``) is refused
    too: listing it would read as permission for space the policy never grants.

    Raises:
        ValueError: An entry is not a CIDR, or overlaps never-reachable space.
    """
    networks: list[IPNetwork] = []
    for entry in entries:
        text = entry.strip()
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise ValueError(f"egress_allow_cidrs entry {entry!r} is not a CIDR range") from exc
        if any(network.overlaps(blocked) for blocked in _NEVER_REACHABLE):
            raise ValueError(
                f"egress_allow_cidrs entry {entry!r} covers link-local, metadata, multicast "
                "or reserved space, which is never reachable"
            )
        networks.append(network)
    return tuple(networks)


@dataclass(frozen=True)
class EgressPolicy:
    """The address rules for one deployment tier.

    The default tier is the strict one, so a caller that forgets to say gets the
    safe answer. ``private_allowlist`` names private ranges the operator reaches on
    purpose (an internal MCP server); it is empty unless set
    (``[tools.policy] egress_allow_cidrs``, see :func:`parse_allow_cidrs`). Never-reachable
    space is judged first, so no allowlist can unlock it.

    ``via_proxy`` is the operator's ``[tools.policy] mcp_via_proxy`` opt-in: connections go
    through the environment's HTTPS proxy, which resolves the name itself, so the
    resolve-then-pin guarantee does not hold for proxied hosts (only literal addresses are
    judged here). Personal tier honors the proxy without the flag.
    """

    tier: Tier = Tier.ENTERPRISE
    private_allowlist: tuple[IPNetwork, ...] = ()
    via_proxy: bool = False
    resolver: Resolver = field(default=system_resolver, compare=False)

    def refusal(self, address: IPAddress) -> str | None:
        """Why ``address`` may not be used, or ``None`` when it may."""
        if always_blocked(address):
            return "link-local, metadata or otherwise unreachable address"
        if self.tier is Tier.PERSONAL:
            return None
        if address.is_loopback:
            return "loopback is reachable at personal tier only"
        if address.is_private and not any(address in net for net in self.private_allowlist):
            return "private address is reachable at personal tier or when allowlisted"
        return None

    @property
    def proxy_allowed(self) -> bool:
        """Whether the environment's proxy may carry connections (opt-in above personal)."""
        return self.via_proxy or self.tier is Tier.PERSONAL

    def check_all(self, host: str, addresses: Sequence[IPAddress]) -> None:
        """Refuse unless EVERY address is allowed (one bad answer taints the lookup)."""
        for address in addresses:
            reason = self.refusal(address)
            if reason is not None:
                raise EgressRefusedError(f"{host} resolves to {address}: {reason}")


__all__ = [
    "METADATA_ADDRESSES",
    "EgressPolicy",
    "EgressRefusedError",
    "Resolver",
    "always_blocked",
    "parse_address",
    "parse_allow_cidrs",
    "system_resolver",
    "unmapped",
]
