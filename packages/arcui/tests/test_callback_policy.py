"""CallbackPolicy: the allowlist + SSRF gate on every ``callback_url`` (SPEC-088 COMP-013).

Contract pinned here for the builder (T-1368), module ``arcui.work_events.policy``
(new package ``arcui/work_events/``):

- ``CallbackPolicy(entries, *, tier, resolver=None)``:
  ``entries: Callable[[], Iterable[AllowlistEntry]]`` is called on EVERY ``check`` (no cache
  that could outlive a removal; production passes the COMP-018 store's ``list``);
  ``tier: Tier`` (``"personal" | "enterprise" | "federal"``);
  ``resolver: arcagent.utils.url_security.Resolver | None`` (``Callable[[str],
  Iterable[str]]``; tests inject one, nothing here touches DNS).
- ``check(url: str) -> AllowlistEntry`` returns the matching ACTIVE entry, else raises
  ``CallbackRefusedError`` (a ``ValueError``) with a stable ``reason``:

  ``callback_not_allowed``  no active entry whose scheme, host and port match exactly
                            (default ports normalized; host case-insensitive);
  ``invalid_callback_url``  malformed, non-http(s), or carries userinfo;
  ``https_required``        http above personal tier;
  ``private_address``       loopback / RFC1918 / ULA / unspecified, literal or resolved,
                            and the entry does not set ``allow_private``;
  ``forbidden_address``     link-local (169.254.0.0/16, fe80::/10, IPv4-mapped forms) or a
                            cloud metadata address/hostname (169.254.169.254,
                            fd00:ec2::254, metadata.google.internal) — refused even with
                            ``allow_private``;
  ``unresolvable_host``     the resolver raises or returns nothing;
  ``allowlist_unavailable`` reading the entries raised (sealed record rolled back,
                            authority down): fail closed.

Requirements: REQ-625 (exact origin, https above personal, link-local/metadata always
refused, private only with allow_private), REQ-632 (rolled-back allowlist fails closed).
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from arctrust import FileJournalAnchor, InProcessSigner
from arctrust.audit import AuditEvent
from arctrust.classification import Classification
from nacl.secret import SecretBox

_PUBLIC_IP = "93.184.216.34"
_REQUESTER = "did:arc:test:user/requester-ada"


# --- helpers -----------------------------------------------------------------------


def _entry(origin: str, *, allow_private: bool = False, status: str = "active") -> Any:
    from arctrust.callback_allowlist import AllowlistEntry

    return AllowlistEntry(
        id=f"{abs(hash((origin, allow_private, status))):032x}"[:32],
        origin=origin,
        allow_private=allow_private,
        clearance=Classification.UNCLASSIFIED,
        status=status,  # type: ignore[arg-type]  # reason: tests feed every lifecycle status
        created_by=_REQUESTER,
        approved_by=_REQUESTER if status == "active" else None,
        created_at=datetime(2026, 10, 10, tzinfo=UTC),
    )


@dataclass
class Resolver:
    """Injected DNS: a fixed table; records every lookup; unknown names fail."""

    table: dict[str, list[str]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def __call__(self, hostname: str) -> Iterable[str]:
        self.calls.append(hostname)
        if hostname not in self.table:
            raise OSError(f"no such host {hostname}")
        return list(self.table[hostname])


@dataclass
class Entries:
    """The fresh-read source: counts reads; the list can change between checks."""

    items: list[Any]
    reads: int = 0

    def __call__(self) -> list[Any]:
        self.reads += 1
        return list(self.items)


def _policy(
    entries: Callable[[], Iterable[Any]] | list[Any],
    *,
    tier: str = "personal",
    resolver: Resolver | None = None,
) -> Any:
    from arcui.work_events.policy import CallbackPolicy

    source = entries if callable(entries) else Entries(list(entries))
    return CallbackPolicy(source, tier=tier, resolver=resolver or Resolver())


def _refused(policy: Any, url: str) -> str:
    from arcui.work_events.policy import CallbackRefusedError

    with pytest.raises(CallbackRefusedError) as excinfo:
        policy.check(url)
    return str(excinfo.value.reason)


def _public(host: str = "hooks.example.com") -> Resolver:
    return Resolver({host: [_PUBLIC_IP]})


# --- fixture smoke -------------------------------------------------------------------


def test_fixture_smoke_resolver_and_entries_doubles() -> None:
    resolver = _public()
    assert list(resolver("hooks.example.com")) == [_PUBLIC_IP]
    with pytest.raises(OSError):
        resolver("unknown.example.com")
    source = Entries(["x"])
    assert source() == ["x"]
    assert source.reads == 1


def test_refused_error_is_a_value_error() -> None:
    from arcui.work_events.policy import CallbackRefusedError

    assert issubclass(CallbackRefusedError, ValueError)


# --- allowlist matching --------------------------------------------------------------


def test_check_returns_the_matching_active_entry() -> None:
    entry = _entry("https://hooks.example.com")
    policy = _policy([entry], resolver=_public())

    assert policy.check("https://hooks.example.com/arc/events?x=1") is entry


def test_check_resolves_through_the_injected_resolver_only() -> None:
    resolver = _public()
    policy = _policy([_entry("https://hooks.example.com")], resolver=resolver)

    policy.check("https://hooks.example.com/cb")

    assert resolver.calls == ["hooks.example.com"]


def test_non_allowlisted_origin_is_refused() -> None:
    policy = _policy([_entry("https://hooks.example.com")], resolver=_public("evil.example.com"))
    assert _refused(policy, "https://evil.example.com/cb") == "callback_not_allowed"


def test_empty_allowlist_refuses_everything() -> None:
    assert _refused(_policy([], resolver=_public()), "https://hooks.example.com/cb") == (
        "callback_not_allowed"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.example.com:8443/cb",  # port differs
        "https://evil.hooks.example.com/cb",  # subdomain
        "https://hooks.example.com.evil.com/cb",  # suffix trick
        "https://hooks.example.co/cb",  # prefix of the host
    ],
)
def test_origin_must_match_scheme_host_and_port_exactly(url: str) -> None:
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or ""
    policy = _policy([_entry("https://hooks.example.com")], resolver=_public(host))
    assert _refused(policy, url) == "callback_not_allowed"


def test_scheme_must_match_exactly_even_at_personal() -> None:
    policy = _policy([_entry("https://hooks.example.com")], resolver=_public())
    assert _refused(policy, "http://hooks.example.com/cb") == "callback_not_allowed"


@pytest.mark.parametrize(
    "url",
    ["https://HOOKS.Example.com/cb", "https://hooks.example.com:443/cb"],
)
def test_host_case_and_default_port_are_normalized(url: str) -> None:
    entry = _entry("https://hooks.example.com")
    assert _policy([entry], resolver=_public()).check(url) is entry


def test_explicit_port_entry_matches_only_that_port() -> None:
    entry = _entry("https://hooks.example.com:8443")
    policy = _policy([entry], resolver=_public())
    assert policy.check("https://hooks.example.com:8443/cb") is entry
    assert _refused(policy, "https://hooks.example.com/cb") == "callback_not_allowed"


@pytest.mark.parametrize("status", ["pending", "removed"])
def test_pending_or_removed_entry_does_not_match(status: str) -> None:
    policy = _policy([_entry("https://hooks.example.com", status=status)], resolver=_public())
    assert _refused(policy, "https://hooks.example.com/cb") == "callback_not_allowed"


# --- fresh read on every check -------------------------------------------------------


def test_entries_are_read_fresh_on_every_check() -> None:
    source = Entries([_entry("https://hooks.example.com")])
    policy = _policy(source, resolver=_public())

    policy.check("https://hooks.example.com/a")
    policy.check("https://hooks.example.com/b")

    assert source.reads == 2


def test_removal_takes_effect_on_the_next_check() -> None:
    source = Entries([_entry("https://hooks.example.com")])
    policy = _policy(source, resolver=_public())
    policy.check("https://hooks.example.com/cb")

    source.items = [_entry("https://hooks.example.com", status="removed")]

    assert _refused(policy, "https://hooks.example.com/cb") == "callback_not_allowed"


def test_entry_source_failure_fails_closed() -> None:
    from arctrust.sealed_state import AuthorityStateError

    def broken() -> list[Any]:
        raise AuthorityStateError("unexplained callbacks authority rollback")

    policy = _policy(broken, resolver=_public())
    assert _refused(policy, "https://hooks.example.com/cb") == "allowlist_unavailable"


# --- scheme by tier ------------------------------------------------------------------


def test_http_is_allowed_at_personal() -> None:
    entry = _entry("http://hooks.example.com")
    assert (
        _policy([entry], tier="personal", resolver=_public()).check("http://hooks.example.com/cb")
        is entry
    )


@pytest.mark.parametrize("tier", ["enterprise", "federal"])
def test_http_is_refused_above_personal_even_when_allowlisted(tier: str) -> None:
    policy = _policy([_entry("http://hooks.example.com")], tier=tier, resolver=_public())
    assert _refused(policy, "http://hooks.example.com/cb") == "https_required"


@pytest.mark.parametrize("tier", ["enterprise", "federal"])
def test_https_is_accepted_above_personal(tier: str) -> None:
    entry = _entry("https://hooks.example.com")
    assert (
        _policy([entry], tier=tier, resolver=_public()).check("https://hooks.example.com/cb")
        is entry
    )


# --- URL shape -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pw@hooks.example.com/cb",
        "https://user@hooks.example.com/cb",
        "https://:pw@hooks.example.com/cb",
    ],
)
def test_userinfo_url_is_refused_even_for_an_allowlisted_host(url: str) -> None:
    policy = _policy([_entry("https://hooks.example.com")], resolver=_public())
    assert _refused(policy, url) == "invalid_callback_url"


@pytest.mark.parametrize(
    "url",
    ["ftp://hooks.example.com/cb", "file:///etc/passwd", "", "https://", "not a url"],
)
def test_malformed_or_non_http_url_is_refused(url: str) -> None:
    policy = _policy([_entry("https://hooks.example.com")], resolver=_public())
    assert _refused(policy, url) == "invalid_callback_url"


def test_at_sign_host_confusion_targets_the_real_host() -> None:
    """``https://hooks.example.com@evil.com`` names host evil.com (and carries userinfo)."""
    policy = _policy(
        [_entry("https://hooks.example.com")],
        resolver=Resolver({"evil.com": [_PUBLIC_IP], "hooks.example.com": [_PUBLIC_IP]}),
    )
    from arcui.work_events.policy import CallbackRefusedError

    with pytest.raises(CallbackRefusedError):
        policy.check("https://hooks.example.com@evil.com/cb")


# --- literal addresses ---------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "10.0.0.5", "192.168.1.10", "[::1]", "0.0.0.0", "[::ffff:127.0.0.1]"],  # noqa: S104  # reason: unsafe target under test
)
def test_private_and_loopback_literals_refused_without_allow_private(host: str) -> None:
    policy = _policy([_entry(f"https://{host}")])
    assert _refused(policy, f"https://{host}/cb") == "private_address"


@pytest.mark.parametrize("host", ["169.254.169.254", "[fe80::1]"])
def test_link_local_literals_refused_without_allow_private(host: str) -> None:
    policy = _policy([_entry(f"https://{host}")])
    assert _refused(policy, f"https://{host}/cb") in {"private_address", "forbidden_address"}


def test_private_literal_accepted_with_allow_private() -> None:
    entry = _entry("https://10.0.0.5", allow_private=True)
    assert _policy([entry]).check("https://10.0.0.5/cb") is entry


def test_loopback_literal_accepted_with_allow_private() -> None:
    entry = _entry("https://127.0.0.1:9000", allow_private=True)
    assert _policy([entry]).check("https://127.0.0.1:9000/cb") is entry


@pytest.mark.parametrize(
    "host",
    [
        "169.254.169.254",
        "169.254.10.20",
        "[fe80::1]",
        "[fd00:ec2::254]",
        "[::ffff:169.254.169.254]",
    ],
)
def test_link_local_and_metadata_literals_refused_even_with_allow_private(host: str) -> None:
    policy = _policy([_entry(f"https://{host}", allow_private=True)])
    assert _refused(policy, f"https://{host}/cb") == "forbidden_address"


# --- resolved hostnames --------------------------------------------------------------


@pytest.mark.parametrize(
    "addresses",
    [["10.1.2.3"], ["127.0.0.1"], [_PUBLIC_IP, "192.168.0.7"], ["::1"], ["fd12::1"]],
)
def test_hostname_resolving_to_a_private_address_is_refused(addresses: list[str]) -> None:
    policy = _policy(
        [_entry("https://intranet.example.com")],
        resolver=Resolver({"intranet.example.com": addresses}),
    )
    assert _refused(policy, "https://intranet.example.com/cb") == "private_address"


def test_hostname_resolving_private_accepted_with_allow_private() -> None:
    entry = _entry("https://intranet.example.com", allow_private=True)
    policy = _policy([entry], resolver=Resolver({"intranet.example.com": ["10.1.2.3"]}))
    assert policy.check("https://intranet.example.com/cb") is entry


@pytest.mark.parametrize(
    "addresses", [["169.254.169.254"], ["fe80::1"], ["10.0.0.1", "169.254.1.1"]]
)
def test_hostname_resolving_link_local_refused_even_with_allow_private(
    addresses: list[str],
) -> None:
    policy = _policy(
        [_entry("https://intranet.example.com", allow_private=True)],
        resolver=Resolver({"intranet.example.com": addresses}),
    )
    assert _refused(policy, "https://intranet.example.com/cb") == "forbidden_address"


@pytest.mark.parametrize("host", ["metadata.google.internal", "METADATA.google.internal"])
def test_metadata_hostname_refused_even_with_allow_private(host: str) -> None:
    """The hostname alone is refused, whatever it resolves to."""
    policy = _policy(
        [_entry(f"https://{host.lower()}", allow_private=True)],
        resolver=Resolver({host.lower(): ["10.0.0.9"]}),
    )
    assert _refused(policy, f"https://{host}/computeMetadata/v1/") == "forbidden_address"


@pytest.mark.parametrize("table", [{}, {"hooks.example.com": []}])
def test_unresolvable_host_is_refused(table: dict[str, list[str]]) -> None:
    policy = _policy([_entry("https://hooks.example.com")], resolver=Resolver(table))
    assert _refused(policy, "https://hooks.example.com/cb") == "unresolvable_host"


# --- integrated with the real sealed store ---------------------------------------------


class _Cipher:
    def __init__(self) -> None:
        self._box = SecretBox(bytes(range(32)))

    def seal(self, payload: bytes) -> str:
        return base64.b64encode(bytes(self._box.encrypt(payload))).decode("ascii")

    def open(self, sealed: str) -> bytes:
        return self._box.decrypt(base64.b64decode(sealed, validate=True))


@dataclass
class _Sink:
    events: list[AuditEvent] = field(default_factory=list)

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def write_durable(self, event: AuditEvent) -> None:
        self.events.append(event)


def _store(tmp_path: Path) -> Any:
    from arctrust.callback_allowlist import CallbackAllowlistStore

    for sub in ("state", "anchor"):
        (tmp_path / sub).mkdir(mode=0o700, exist_ok=True)
    sink = _Sink()
    return CallbackAllowlistStore(
        tmp_path / "state" / "callbacks.json",
        anchor=FileJournalAnchor(
            tmp_path / "anchor", scope="test/callbacks", signer=InProcessSigner(bytes(range(32)))
        ),
        cipher=_Cipher(),
        audit_sink=sink,
        strict_audit_sink=sink,
        actor_did=_REQUESTER,
        tier="personal",
    )


def test_real_store_removal_is_refused_on_the_next_check(tmp_path: Path) -> None:
    from arctrust.callback_allowlist import NewCallbackEntry

    store = _store(tmp_path)
    entry = store.request(NewCallbackEntry(origin="https://hooks.example.com"), _REQUESTER)
    policy = _policy(store.list, resolver=_public())
    assert policy.check("https://hooks.example.com/cb").id == entry.id

    store.remove(entry.id, _REQUESTER)

    assert _refused(policy, "https://hooks.example.com/cb") == "callback_not_allowed"


def test_real_store_restored_older_record_makes_every_check_refuse(tmp_path: Path) -> None:
    from arctrust.callback_allowlist import NewCallbackEntry

    store = _store(tmp_path)
    kept = store.request(NewCallbackEntry(origin="https://kept.example.com"), _REQUESTER)
    gone = store.request(NewCallbackEntry(origin="https://hooks.example.com"), _REQUESTER)
    before_remove = store.path.read_bytes()
    store.remove(gone.id, _REQUESTER)
    store.request(NewCallbackEntry(origin="https://later.example.com"), _REQUESTER)
    store.path.write_bytes(before_remove)

    policy = _policy(
        store.list,
        resolver=Resolver({"hooks.example.com": [_PUBLIC_IP], "kept.example.com": [_PUBLIC_IP]}),
    )

    assert kept.status == "active"
    assert _refused(policy, "https://hooks.example.com/cb") == "allowlist_unavailable"
    assert _refused(policy, "https://kept.example.com/cb") == "allowlist_unavailable"
