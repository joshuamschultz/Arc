"""Canonical outbound HTTP URL validation and SSRF protection."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

Resolver = Callable[[str], Iterable[str]]
_resolver_override: ContextVar[Resolver | None] = ContextVar("arcagent_url_resolver", default=None)


@dataclass(frozen=True, slots=True)
class ValidatedURL:
    """Normalized fields from a syntactically and network-safe URL."""

    raw: str
    parsed: SplitResult
    hostname: str


class UnsafeURLError(ValueError):
    """Raised when an outbound URL could reach an unsafe destination."""


def _system_resolver(hostname: str) -> Iterable[str]:
    addresses: Iterator[str] = (
        str(item[4][0]) for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    )
    return set(addresses)


def _is_unsafe_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # ``is_global`` excludes private, loopback, link-local, multicast,
    # unspecified, reserved, and documentation-only address space.
    return (
        not address.is_global
        or address.is_multicast
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_reserved
        or address.is_unspecified
    )


def validate_http_url(
    url: str,
    *,
    resolve: bool = False,
    resolver: Resolver | None = None,
) -> ValidatedURL:
    """Validate an absolute HTTP(S) URL and optionally resolve its host.

    Resolution is fail-closed and every returned address must be globally
    routable. Call this immediately before egress and again for redirects.
    """
    if not url or any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise UnsafeURLError("URL is empty or contains control characters")

    try:
        parsed = urlsplit(url)
        port = parsed.port  # Force validation of malformed/out-of-range ports.
    except ValueError as exc:
        raise UnsafeURLError("URL is malformed") from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeURLError("Only http and https URLs are permitted")
    if not parsed.netloc or not parsed.hostname:
        raise UnsafeURLError("URL must contain a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("URL credentials are not permitted")
    if port is not None and port <= 0:
        raise UnsafeURLError("URL port is invalid")

    hostname = parsed.hostname.rstrip(".").lower()
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and _is_unsafe_address(literal):
        raise UnsafeURLError("URL resolves to a non-public address")

    if resolve:
        active_resolver = resolver or _resolver_override.get() or _system_resolver
        try:
            addresses = tuple(active_resolver(hostname))
        except (OSError, ValueError) as exc:
            raise UnsafeURLError("URL hostname could not be resolved safely") from exc
        if not addresses:
            raise UnsafeURLError("URL hostname did not resolve")
        try:
            unsafe = any(_is_unsafe_address(ipaddress.ip_address(item)) for item in addresses)
        except ValueError as exc:
            raise UnsafeURLError("URL hostname returned an invalid address") from exc
        if unsafe:
            raise UnsafeURLError("URL resolves to a non-public address")

    return ValidatedURL(raw=url, parsed=parsed, hostname=hostname)


def set_url_resolver(resolver: Resolver | None) -> None:
    """Override DNS resolution in the current context for host integrations/tests."""
    _resolver_override.set(resolver)


__all__ = [
    "Resolver",
    "UnsafeURLError",
    "ValidatedURL",
    "set_url_resolver",
    "validate_http_url",
]
