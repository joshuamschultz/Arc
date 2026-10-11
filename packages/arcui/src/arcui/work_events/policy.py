"""CallbackPolicy: the allowlist and SSRF gate on every ``callback_url`` (SPEC-088 COMP-013).

:meth:`CallbackPolicy.check` accepts a callback URL only when all of these hold:

1. it is an absolute ``http``/``https`` URL with no userinfo;
2. above personal tier it is ``https``;
3. an ACTIVE allowlist entry has exactly its origin (scheme, host, port; the
   default port and host case normalized by :func:`arctrust.callback_allowlist.normalize_origin`);
4. no address it names or resolves to is link-local or a cloud metadata
   endpoint (always refused), and none is private, loopback or unspecified
   unless the matched entry sets ``allow_private``.

The allowlist is read from its source on EVERY check, so a removal takes effect
on the next callback and a rolled-back authority record refuses everything.
Any failure fails closed with a :class:`CallbackRefusedError` carrying a stable
``reason``. DNS goes through the injected resolver; ``check`` blocks (allowlist
read + DNS), so async callers run it off the event loop.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from arctrust.callback_allowlist import AllowlistEntry, CallbackOriginError, normalize_origin

from arcui.principals import Tier

__all__ = [
    "CallbackPolicy",
    "CallbackRefusedError",
    "EntrySource",
    "Resolver",
    "checked_addresses",
    "system_resolver",
]

logger = logging.getLogger(__name__)

#: ``hostname -> addresses``; the same shape as ``arcagent.utils.url_security.Resolver``.
Resolver = Callable[[str], Iterable[str]]
#: Zero-arg reader of the current allowlist (production: the store's ``list``).
EntrySource = Callable[[], Iterable[AllowlistEntry]]

_Address = ipaddress.IPv4Address | ipaddress.IPv6Address

_METADATA_HOSTNAMES = frozenset({"metadata.google.internal", "metadata"})
_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS, GCP, Azure, OCI
        ipaddress.ip_address("169.254.170.2"),  # AWS ECS task metadata
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IPv6 IMDS (a ULA, not link-local)
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud
    }
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class CallbackRefusedError(ValueError):
    """A callback URL was refused; ``reason`` is a stable code, never the URL."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def system_resolver(hostname: str) -> Iterable[str]:
    """The host's DNS answer for ``hostname`` (blocking: call off the event loop)."""
    return {
        str(info[4][0]) for info in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    }


def _origin_of(url: str) -> str:
    """The canonical origin of ``url``, or ``invalid_callback_url``."""
    if not url or any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise CallbackRefusedError("invalid_callback_url")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise CallbackRefusedError("invalid_callback_url") from None
    # Any userinfo, even empty, is refused: ``https://good@evil`` names host evil.
    if parts.scheme.lower() not in {"http", "https"} or "@" in parts.netloc:
        raise CallbackRefusedError("invalid_callback_url")
    try:
        return normalize_origin(f"{parts.scheme}://{parts.netloc}")
    except CallbackOriginError:
        raise CallbackRefusedError("invalid_callback_url") from None


def _embedded_ipv4(address: _Address) -> _Address:
    """The IPv4 address an IPv6 transition form carries, else ``address`` itself."""
    if isinstance(address, ipaddress.IPv4Address):
        return address
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address in _NAT64:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address


def _is_forbidden(address: _Address) -> bool:
    """Link-local, multicast or a metadata endpoint: refused even with allow_private."""
    for candidate in {address, _embedded_ipv4(address)}:
        if candidate in _METADATA_ADDRESSES or candidate.is_link_local or candidate.is_multicast:
            return True
    return False


def _is_private(address: _Address) -> bool:
    """Not globally routable (loopback, RFC1918, ULA, unspecified, CGNAT, reserved)."""
    return not address.is_global or not _embedded_ipv4(address).is_global


class CallbackPolicy:
    """Decide whether a ``callback_url`` may receive work-event callbacks."""

    def __init__(
        self, entries: EntrySource, *, tier: Tier, resolver: Resolver | None = None
    ) -> None:
        self._entries = entries
        self._tier = tier
        self._resolver = resolver or system_resolver

    def check(self, url: str) -> AllowlistEntry:
        """Return the active entry allowing ``url``, else raise :class:`CallbackRefusedError`."""
        origin = _origin_of(url)
        if origin.startswith("http://") and self._tier != "personal":
            raise CallbackRefusedError("https_required")
        entry = self._active_entry(origin)
        self._check_addresses(origin.partition("://")[2], allow_private=entry.allow_private)
        return entry

    def _active_entry(self, origin: str) -> AllowlistEntry:
        try:
            entries = list(self._entries())
        except Exception as exc:  # reason: any authority failure must fail closed
            logger.error("callback_policy allowlist_unavailable class=%s", type(exc).__name__)
            raise CallbackRefusedError("allowlist_unavailable") from None
        for entry in entries:
            if entry.status == "active" and entry.origin == origin:
                return entry
        raise CallbackRefusedError("callback_not_allowed")

    def _check_addresses(self, netloc: str, *, allow_private: bool) -> None:
        host = urlsplit(f"//{netloc}").hostname or ""
        checked_addresses(host, self._resolver, allow_private=allow_private)


def checked_addresses(
    host: str, resolver: Resolver, *, allow_private: bool
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address ``host`` names or resolves to, each one allowed, else refused.

    Resolves at most once. Link-local, multicast and metadata addresses are
    always refused; private ones unless ``allow_private``. The callback
    dispatcher connects to an address from this list, never to the name again.
    """
    if host in _METADATA_HOSTNAMES:
        raise CallbackRefusedError("forbidden_address")
    addresses = _addresses(host, resolver)
    if any(_is_forbidden(address) for address in addresses):
        raise CallbackRefusedError("forbidden_address")
    if not allow_private and any(_is_private(address) for address in addresses):
        raise CallbackRefusedError("private_address")
    return addresses


def _addresses(host: str, resolver: Resolver) -> list[_Address]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass  # a DNS name: resolve it below
    try:
        addresses = [ipaddress.ip_address(item) for item in resolver(host)]
    except (OSError, ValueError, UnicodeError):
        raise CallbackRefusedError("unresolvable_host") from None
    if not addresses:
        raise CallbackRefusedError("unresolvable_host")
    return addresses
