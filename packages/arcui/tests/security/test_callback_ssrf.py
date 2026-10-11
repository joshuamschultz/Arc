"""SPEC-088 T-1375: callback SSRF, signing-key and allowlist abuse (COMP-017, REQ-632).

REQ-625 (exact-origin allowlist in a sealed record, https above personal,
link-local and metadata always refused, private only with ``allow_private``,
re-checked before every delivery, connect to the checked address, no
redirects), REQ-626 (signed only with the dedicated work-events key, never the
operator key; no key -> nothing sent), REQ-629 (classification withheld),
REQ-631 (audited), REQ-632 (internal addresses incl. DNS rebinding, a callback
signed with another key, a rolled-back allowlist).

Real components end to end: the sealed, anchored ``CallbackAllowlistStore``
(``test_callback_allowlist_routes``'s rig), the real ``CallbackPolicy`` reading
it on every check, the real ``CallbackDispatcher`` over the real
``WorkEventLog``, the real ``WorkEventSigner`` minted in an out-of-process
notary keystore beside the operator key, and the full ``create_app`` with the
real ``AuthMiddleware`` for the routes. Doubles sit only at the edges: DNS (a
scripted resolver), the receiver (``httpx.MockTransport``), the clock, the
operator notifier and the audit sinks.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.work_events import WorkEvent, WorkEventLog
from arctrust import FileNotaryTransit, verify_signature
from arctrust.audit import AuditEvent
from arctrust.callback_allowlist import NewCallbackEntry
from arctrust.classification import Classification
from arctrust.operator_resolver import OPERATOR_KEY_REF, MachineSecurity
from arctrust.principals import Scope
from arctrust.users import OPERATOR
from arctrust.work_event_signer import (
    WorkEventSigner,
    provision_work_event_key,
    work_event_signer_for,
)
from packages.arctrust.tests.unit.test_work_event_signer import _reachable_values
from packages.arcui.tests.principal_fixtures import (
    OPERATOR_TOKEN,
    VIEWER_TOKEN,
    PrincipalRig,
    SpyWorm,
    agent_did,
    all_scopes,
    issue_principal,
    principal_rig,
    write_team,
)
from packages.arcui.tests.test_callback_allowlist_routes import CallbackRig, _callback_rig
from packages.arcui.tests.user_authority import user_store_factory

from arcui.work_events import CallbackPolicy, CallbackRefusedError
from arcui.work_events.dispatcher import CallbackDispatcher

HOST = "hooks.example.com"
ORIGIN = f"https://{HOST}"
URL = f"{ORIGIN}/arc/callback"
PUBLIC_IP = "93.184.216.34"
PUBLIC_IP_2 = "93.184.216.35"
ACTOR = "did:arc:test:operator/callback-admin"
SALES = agent_did()
DOMAIN = b"arc.work-event.v1"
ACCEPTING = "\n[modules.tasks]\nenabled = true\n\n[modules.tasks.config]\ndispatch = true\n"
CALLBACKS = "/api/work-events/callbacks"


# --- edge doubles ------------------------------------------------------------------------


@dataclass
class Dns:
    """Scripted DNS: each name answers from its list in order, repeating the last answer."""

    table: dict[str, list[list[str]]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def answer(self, host: str, *answers: list[str]) -> None:
        self.table[host] = list(answers)

    def __call__(self, hostname: str) -> Iterable[str]:
        self.calls.append(hostname)
        answers = self.table.get(hostname)
        if not answers:
            raise OSError(f"no such host {hostname}")
        seen = self.calls.count(hostname)
        return list(answers[min(seen, len(answers)) - 1])


@dataclass
class Receiver:
    """The callback endpoint; records every request the dispatcher hands the transport."""

    reply: Callable[[httpx.Request], httpx.Response] = lambda _r: httpx.Response(204)
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.reply(request)


@dataclass
class Audit:
    events: list[AuditEvent] = field(default_factory=list)

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def outcomes(self, event_id: str) -> list[tuple[str, Any]]:
        return [
            (e.outcome, (e.extra or {}).get("reason"))
            for e in self.events
            if e.action == "work_event.callback" and event_id in e.target
        ]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def _notify(_event_id: str) -> None:
    return None


# --- custody: the operator key and the work-events key in one notary keystore -------------


@dataclass
class Custody:
    security: MachineSecurity
    signer: WorkEventSigner
    operator_seed: bytes

    @property
    def notary(self) -> FileNotaryTransit:
        return FileNotaryTransit(Path(self.security.notary_keystore), "ed25519")

    @property
    def operator_public(self) -> bytes:
        return self.notary.public_key(OPERATOR_KEY_REF)

    def operator_sign(self, message: bytes) -> bytes:
        return self.notary.sign(OPERATOR_KEY_REF, message)


@pytest.fixture
def custody(tmp_path: Path) -> Custody:
    keystore = tmp_path / "notary"
    seed = os.urandom(32)
    FileNotaryTransit.provision(keystore, OPERATOR_KEY_REF, seed, "ed25519")
    security = MachineSecurity(
        custody="vault_transit", signing_algorithm="ed25519", notary_keystore=str(keystore)
    )
    provision_work_event_key(security)
    signer = work_event_signer_for(security)
    assert signer is not None
    return Custody(security=security, signer=signer, operator_seed=seed)


# --- the allowlist, the policy and the dispatcher -------------------------------------------


@dataclass
class Wire:
    rig: CallbackRig
    dns: Dns
    log: WorkEventLog
    clock: Clock
    receiver: Receiver
    audit: Audit
    signer: Any

    def allow(
        self,
        origin: str = ORIGIN,
        *,
        allow_private: bool = False,
        clearance: Classification = Classification.UNCLASSIFIED,
    ) -> str:
        entry = self.rig.open().request(
            NewCallbackEntry(origin, allow_private=allow_private, clearance=clearance), ACTOR
        )
        return entry.id

    def policy(self, tier: Any = "personal") -> CallbackPolicy:
        return CallbackPolicy(lambda: self.rig.open().list(), tier=tier, resolver=self.dns)

    def dispatcher(self, tier: str = "personal") -> CallbackDispatcher:
        return CallbackDispatcher(
            self.log,
            signer=self.signer,
            policy=self.policy(tier),
            transport=httpx.MockTransport(self.receiver),
            resolver=self.dns,
            notify_operator=_notify,
            audit_sink=self.audit,
            clock=self.clock,
        )

    async def event(
        self, url: str = URL, *, classification: str = "UNCLASSIFIED", agent: str = SALES
    ) -> str:
        event_id = f"evt-{uuid.uuid4().hex}"
        assert await self.log.append(
            WorkEvent(
                event_id=event_id,
                kind="run.finished",
                occurred_at="2026-10-10T12:00:00+00:00",
                agent_did=agent,
                run_id=str(uuid.uuid4()),
                status="done",
                classification=classification,
                callback_url=url,
            )
        )
        return event_id

    async def deliver(self, url: str = URL, *, tier: str = "personal", **event: Any) -> str:
        event_id = await self.event(url, **event)
        assert await self.dispatcher(tier).dispatch_once() == 1
        return event_id


@pytest.fixture
async def wire(tmp_path: Path, custody: Custody) -> AsyncIterator[Wire]:
    backend = FakeBackend()
    await backend.start()
    clock = Clock()
    dns = Dns()
    dns.answer(HOST, [PUBLIC_IP])
    yield Wire(
        rig=_callback_rig(tmp_path / "authority", "personal"),
        dns=dns,
        log=WorkEventLog(backend, clock=clock),
        clock=clock,
        receiver=Receiver(),
        audit=Audit(),
        signer=custody.signer,
    )
    await backend.stop()


def _refused(policy: CallbackPolicy, url: str) -> str:
    with pytest.raises(CallbackRefusedError) as raised:
        policy.check(url)
    return raised.value.reason


# ======================================================================================
# Internal addresses: the allowlist matches, the address check must still refuse
# ======================================================================================

PRIVATE_LITERALS = [
    "http://127.0.0.1:8080",
    "http://[::1]",
    "http://10.0.0.5",
    "http://172.16.4.4",
    "http://192.168.1.1",
    "http://0.0.0.0",
    "http://100.64.0.1",
    "http://[fc00::1]",
    "http://[::ffff:127.0.0.1]",
    "http://[::ffff:10.0.0.1]",
    "http://[2002:7f00:1::]",
    "http://[64:ff9b::7f00:1]",
]
FORBIDDEN_LITERALS = [
    "http://169.254.169.254",
    "http://169.254.170.2",
    "http://100.100.100.200",
    "http://169.254.1.1",
    "http://[fe80::1]",
    "http://[fd00:ec2::254]",
    "http://[::ffff:169.254.169.254]",
    "http://[64:ff9b::a9fe:a9fe]",
    "http://[2002:a9fe:a9fe::]",
]
# DNS names whose answer is internal: (host, answer, reason without allow_private).
INTERNAL_NAMES = [
    ("localhost", ["127.0.0.1", "::1"], "private_address"),
    ("intranet.example.test", ["10.1.2.3"], "private_address"),
    ("mixed.example.test", [PUBLIC_IP, "192.168.0.10"], "private_address"),
    ("imds.example.test", ["169.254.169.254"], "forbidden_address"),
    ("mapped.example.test", ["::ffff:169.254.169.254"], "forbidden_address"),
    ("nat64.example.test", ["64:ff9b::a9fe:a9fe"], "forbidden_address"),
]


class TestInternalAddresses:
    @pytest.mark.parametrize("origin", PRIVATE_LITERALS)
    def test_private_literal_refused_without_allow_private(self, wire: Wire, origin: str) -> None:
        wire.allow(origin)

        assert _refused(wire.policy(), f"{origin}/cb") == "private_address"

    @pytest.mark.parametrize("origin", FORBIDDEN_LITERALS)
    @pytest.mark.parametrize("allow_private", [False, True])
    def test_link_local_and_metadata_refused_even_with_allow_private(
        self, wire: Wire, origin: str, allow_private: bool
    ) -> None:
        wire.allow(origin, allow_private=allow_private)

        assert _refused(wire.policy(), f"{origin}/latest/meta-data/") == "forbidden_address"

    @pytest.mark.parametrize("host", ["metadata.google.internal", "metadata"])
    def test_metadata_hostname_refused_even_with_allow_private(
        self, wire: Wire, host: str
    ) -> None:
        wire.allow(f"http://{host}", allow_private=True)
        wire.dns.answer(host, [PUBLIC_IP])

        assert _refused(wire.policy(), f"http://{host}/computeMetadata/v1/") == (
            "forbidden_address"
        )

    @pytest.mark.parametrize(("host", "answer", "reason"), INTERNAL_NAMES)
    def test_name_resolving_internal_is_refused(
        self, wire: Wire, host: str, answer: list[str], reason: str
    ) -> None:
        wire.allow(f"https://{host}")
        wire.dns.answer(host, answer)

        assert _refused(wire.policy(), f"https://{host}/cb") == reason

    @pytest.mark.parametrize("origin", ["http://169.254.169.254", "http://[::ffff:a9fe:a9fe]"])
    async def test_dispatcher_never_sends_to_a_metadata_address(
        self, wire: Wire, origin: str
    ) -> None:
        wire.allow(origin, allow_private=True)

        event_id = await wire.deliver(f"{origin}/latest/meta-data/")

        assert wire.receiver.requests == []
        assert wire.audit.outcomes(event_id) == [("retry", "forbidden_address")]


# ======================================================================================
# DNS rebinding between the check and the connect
# ======================================================================================


class TestDnsRebinding:
    async def test_answer_turning_internal_after_the_check_sends_nothing(self, wire: Wire) -> None:
        wire.allow()
        wire.dns.answer(HOST, [PUBLIC_IP], ["127.0.0.1"])

        event_id = await wire.deliver()

        assert wire.receiver.requests == []
        assert wire.audit.outcomes(event_id) == [("retry", "private_address")]

    async def test_connect_uses_the_checked_address_never_a_later_answer(self, wire: Wire) -> None:
        wire.allow()
        wire.dns.answer(HOST, [PUBLIC_IP], [PUBLIC_IP_2], ["169.254.169.254"])

        event_id = await wire.deliver()

        (request,) = wire.receiver.requests
        assert request.url.host == PUBLIC_IP_2, "the transport was handed a name to resolve"
        assert request.headers["host"] == HOST
        assert request.extensions.get("sni_hostname") == HOST
        assert wire.dns.calls.count(HOST) == 2, "resolved again after the address was pinned"
        assert wire.audit.outcomes(event_id) == [("delivered", None)]

    async def test_intake_check_passing_does_not_bless_a_later_delivery(self, wire: Wire) -> None:
        """The intake check saw a public answer; delivery re-checks and refuses."""
        wire.allow()
        wire.dns.answer(HOST, [PUBLIC_IP], ["10.0.0.7"])
        wire.policy().check(URL)  # the check intake makes, before the row exists

        event_id = await wire.deliver()

        assert wire.receiver.requests == []
        assert wire.audit.outcomes(event_id) == [("retry", "private_address")]


# ======================================================================================
# Redirects are never followed
# ======================================================================================


class TestRedirects:
    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    async def test_redirect_to_an_internal_address_is_not_followed(
        self, wire: Wire, status: int
    ) -> None:
        wire.allow()
        wire.receiver.reply = lambda _r: httpx.Response(
            status, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
        )

        event_id = await wire.deliver()

        assert len(wire.receiver.requests) == 1
        assert wire.receiver.requests[0].url.host == PUBLIC_IP
        assert wire.audit.outcomes(event_id) == [("retry", f"http_{status}")]


# ======================================================================================
# Only an exact, active allowlisted origin
# ======================================================================================

LOOKALIKES = [
    "https://hooks.example.com.evil.test/cb",
    "https://evil.test/cb",
    "https://evilhooks.example.com/cb",
    "https://sub.hooks.example.com/cb",
    "https://hooks.example.com:8443/cb",
    "http://hooks.example.com/cb",
    "https://hooks.example.co/cb",
    "https://hooks.example.com@evil.test/cb",
    "https://evil.test#@hooks.example.com/cb",
    "https://evil.test?@hooks.example.com/cb",
    "https://evil.test\\@hooks.example.com/cb",
    "ftp://hooks.example.com/cb",
    "//hooks.example.com/cb",
    "https://hooks.example.com\t.evil.test/cb",
]


class TestNonAllowlistedOrigin:
    @pytest.mark.parametrize("url", LOOKALIKES)
    def test_lookalike_origin_is_refused(self, wire: Wire, url: str) -> None:
        wire.allow()
        for host in ("evil.test", "hooks.example.com.evil.test", "evilhooks.example.com"):
            wire.dns.answer(host, [PUBLIC_IP])
        wire.dns.answer("sub.hooks.example.com", [PUBLIC_IP])
        wire.dns.answer("hooks.example.co", [PUBLIC_IP])

        assert _refused(wire.policy(), url) in {"callback_not_allowed", "invalid_callback_url"}

    def test_exact_origin_with_case_and_default_port_is_accepted(self, wire: Wire) -> None:
        """Control: the refusals above are the origin match, not a broken policy."""
        wire.allow()

        assert wire.policy().check("https://HOOKS.example.com:443/any/path").origin == ORIGIN

    async def test_lookalike_is_refused_again_at_delivery(self, wire: Wire) -> None:
        wire.allow()
        wire.dns.answer("evil.test", [PUBLIC_IP])

        event_id = await wire.deliver("https://evil.test/cb")

        assert wire.receiver.requests == []
        assert wire.audit.outcomes(event_id) == [("retry", "callback_not_allowed")]

    async def test_origin_removed_after_intake_is_refused_at_delivery(self, wire: Wire) -> None:
        entry_id = wire.allow()
        wire.policy().check(URL)
        wire.rig.open().remove(entry_id, ACTOR)

        event_id = await wire.deliver()

        assert wire.receiver.requests == []
        assert wire.audit.outcomes(event_id) == [("retry", "callback_not_allowed")]


# ======================================================================================
# https above personal
# ======================================================================================


class TestHttpAbovePersonal:
    @pytest.mark.parametrize("tier", ["enterprise", "federal"])
    async def test_http_entry_is_refused_above_personal(self, wire: Wire, tier: str) -> None:
        wire.allow("http://hooks.example.com")

        reason = _refused(wire.policy(tier), "http://hooks.example.com/cb")
        event_id = await wire.deliver("http://hooks.example.com/cb", tier=tier)

        assert reason == "https_required"
        assert wire.receiver.requests == []
        assert wire.audit.outcomes(event_id) == [("retry", "https_required")]


# ======================================================================================
# Classification withheld
# ======================================================================================


class TestClassificationWithheld:
    @pytest.mark.parametrize("label", ["SECRET", "CUI", "TOP_SECRET", "SECRET//NOFORN", "?"])
    async def test_event_above_the_entry_clearance_is_withheld(
        self, wire: Wire, label: str
    ) -> None:
        wire.allow()

        event_id = await wire.deliver(classification=label)

        assert wire.receiver.requests == []
        assert wire.audit.outcomes(event_id) == [("withheld", "above_clearance")]

    async def test_event_at_the_entry_clearance_is_delivered(self, wire: Wire) -> None:
        """Control: the entry's clearance, read from the sealed record, admits its level."""
        wire.allow(clearance=Classification.SECRET)

        event_id = await wire.deliver(classification="SECRET")

        assert len(wire.receiver.requests) == 1
        assert wire.audit.outcomes(event_id) == [("delivered", None)]

    async def test_withheld_callback_body_never_carries_a_result(self, wire: Wire) -> None:
        wire.allow(clearance=Classification.SECRET)

        await wire.deliver(classification="SECRET")

        body = json.loads(wire.receiver.requests[0].content)
        assert not {"result", "output", "payload", "callback_url"} & set(body)


# ======================================================================================
# Signing: only the published work-events key verifies a callback
# ======================================================================================


def _message(timestamp: str, event_id: str, body: bytes) -> bytes:
    return b"\n".join(
        (DOMAIN, timestamp.encode(), event_id.encode(), hashlib.sha256(body).hexdigest().encode())
    )


def _signature(request: httpx.Request) -> tuple[str, bytes]:
    algorithm, _, encoded = request.headers["Arc-Signature"].partition("=")
    return algorithm, base64.b64decode(encoded, validate=True)


# ======================================================================================
# The deployment: create_app with the allowlist, the signer and the real middleware
# ======================================================================================


@dataclass
class Deployment:
    app: Any
    client: httpx.AsyncClient
    worm: SpyWorm
    rig: CallbackRig
    principals: PrincipalRig
    operator: dict[str, str]

    def refusals(self) -> list[Any]:
        return [r for r in self.worm.written if r.outcome in ("denied", "error")]

    async def tasks(self) -> list[Any]:
        return list(await self.app.state.task_store.list())

    async def intake(self, callback_url: str, key: str = "k-1") -> httpx.Response:
        return await self.client.post(
            "/api/runs",
            json={"agent": "sales", "task": "notify me", "callback_url": callback_url},
            headers={**self.operator, "Idempotency-Key": key},
        )


async def _deployment(
    tmp_path: Path, signer: Any, *, wire: Wire | None = None, tier: str = "personal"
) -> Deployment:
    from arcui.auth import AuthConfig
    from arcui.principals import PrincipalsConfig
    from arcui.server import _start_callback_dispatcher, create_app

    root = tmp_path / "deployment"
    users = user_store_factory(root / "accounts")
    principals = principal_rig(root / "authority")
    rig = wire.rig if wire is not None else _callback_rig(root / "callbacks", tier)
    team = write_team(root / "team", {"sales": SALES})
    toml = team / "sales_agent" / "arcagent.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + ACCEPTING, encoding="utf-8")
    auth = AuthConfig({"operator_token": OPERATOR_TOKEN, "viewer_token": VIEWER_TOKEN})
    app = create_app(
        auth_config=auth,
        user_store_factory=users,
        team_root=team,
        data_dir=root / "arcui-data",
        principal_store_factory=principals.factory,
        principals_config=PrincipalsConfig(tier=tier),
        callback_allowlist_factory=rig.open,
        work_event_signer=signer,
    )
    worm = SpyWorm()
    app.state.audit_worm = worm
    await app.state.arcstore_backend.start()
    if wire is not None:
        # The lifespan's dispatcher with the edges injected: scripted DNS, the receiver.
        app.state.callback_policy = wire.policy(tier)
        app.state.callback_dispatcher = wire.dispatcher(tier)
    else:
        assert await _start_callback_dispatcher(app, None) is None
    user = users().add("ada@example.com", "correct-horse-battery", roles=(OPERATOR,))
    session = auth.sessions.issue(email=user.email, did=user.did, role="operator")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://arc")
    return Deployment(
        app=app,
        client=client,
        worm=worm,
        rig=rig,
        principals=principals,
        operator={"Authorization": f"Bearer {session.token}"},
    )


@pytest.fixture
async def deployment(
    tmp_path: Path, wire: Wire, custody: Custody, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Deployment]:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    built = await _deployment(tmp_path, custody.signer, wire=wire)
    yield built
    await built.client.aclose()


class TestOperatorKeyNeverSignsCallbacks:
    async def test_operator_signed_callback_fails_against_the_published_key(
        self, deployment: Deployment, custody: Custody
    ) -> None:
        published = (
            await deployment.client.get("/api/work-events/keys", headers=deployment.operator)
        ).json()
        public_key = base64.b64decode(published["public_key"])
        body = json.dumps({"event_id": "evt-forged", "kind": "run.finished"}).encode()
        message = _message("1760097600", "evt-forged", body)

        forged = custody.operator_sign(message)

        assert published["algorithm"] == "ed25519"
        assert public_key != custody.operator_public, "the operator key is the published key"
        assert not verify_signature("ed25519", message, forged, public_key)
        assert verify_signature("ed25519", message, forged, custody.operator_public)

    async def test_dispatched_callback_verifies_only_with_the_published_key(
        self, deployment: Deployment, wire: Wire, custody: Custody
    ) -> None:
        wire.allow()
        published = (
            await deployment.client.get("/api/work-events/keys", headers=deployment.operator)
        ).json()
        public_key = base64.b64decode(published["public_key"])

        await wire.deliver()

        (request,) = wire.receiver.requests
        algorithm, signature = _signature(request)
        message = _message(
            request.headers["Arc-Timestamp"], request.headers["Arc-Event-Id"], request.content
        )
        assert request.headers["Arc-Key-Id"] == published["key_id"]
        assert published["key_id"] == hashlib.sha256(public_key).hexdigest()[:16]
        assert verify_signature(algorithm, message, signature, public_key)
        assert not verify_signature(algorithm, message, signature, custody.operator_public)

    def test_signer_exposes_no_operator_material(self, custody: Custody) -> None:
        public = {name for name in dir(custody.signer) if not name.startswith("_")}
        reachable = list(_reachable_values(custody.signer))

        assert public == {"algorithm", "key_id", "public_key", "sign"}
        assert custody.signer.public_key != custody.operator_public
        assert not [
            value
            for value in reachable
            if isinstance(value, (bytes, bytearray))
            and (custody.operator_seed in bytes(value) or bytes(value) == custody.operator_public)
        ]

    def test_signer_signatures_never_verify_under_the_operator_key(self, custody: Custody) -> None:
        message = _message("1760097600", "evt-1", b"{}")

        signature = custody.signer.sign(message)

        assert verify_signature("ed25519", message, signature, custody.signer.public_key)
        assert not verify_signature("ed25519", message, signature, custody.operator_public)


class TestSignerAbsent:
    async def test_intake_with_a_callback_while_the_signer_is_absent_is_503_and_sends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
        built = await _deployment(tmp_path, None)
        built.rig.open().request(NewCallbackEntry(ORIGIN), ACTOR)
        before = len(built.refusals())

        resp = await built.intake(URL)
        keys = await built.client.get("/api/work-events/keys", headers=built.operator)

        assert resp.status_code == 503, resp.text
        assert resp.json() == {"error": "notifications_unavailable"}
        assert len(built.refusals()) - before == 1
        assert await built.tasks() == []
        assert await built.app.state.work_event_log.read(0, limit=10) == []
        assert built.app.state.callback_dispatcher is None
        assert keys.status_code == 503
        await built.client.aclose()


# ======================================================================================
# Internal callback URLs are refused at intake (route level)
# ======================================================================================


class TestIntakeRefusesInternalCallbacks:
    @pytest.mark.parametrize(
        "origin",
        ["http://127.0.0.1:9000", "http://169.254.169.254", "http://[::ffff:169.254.169.254]"],
    )
    async def test_internal_callback_is_400_one_row_no_run(
        self, deployment: Deployment, wire: Wire, origin: str
    ) -> None:
        wire.allow(origin)
        before = len(deployment.refusals())

        resp = await deployment.intake(f"{origin}/cb")

        assert resp.status_code == 400, resp.text
        assert resp.json() == {"error": "callback_not_allowed"}
        assert len(deployment.refusals()) - before == 1
        assert await deployment.tasks() == []

    async def test_non_allowlisted_callback_is_400_no_run(
        self, deployment: Deployment, wire: Wire
    ) -> None:
        wire.allow()
        wire.dns.answer("evil.test", [PUBLIC_IP])

        resp = await deployment.intake("https://evil.test/cb")

        assert resp.status_code == 400, resp.text
        assert await deployment.tasks() == []

    async def test_allowlisted_callback_is_accepted(
        self, deployment: Deployment, wire: Wire
    ) -> None:
        """Control: the refusals above are the callback policy, not a broken intake."""
        wire.allow()

        resp = await deployment.intake(URL)

        assert resp.status_code == 202, resp.text


# ======================================================================================
# A restored older allowlist record is detected and every check refuses
# ======================================================================================


class TestRolledBackAllowlist:
    async def test_restored_record_refuses_every_check_route_and_delivery(
        self, deployment: Deployment, wire: Wire
    ) -> None:
        store = wire.rig.open()
        removed = store.request(NewCallbackEntry(ORIGIN), ACTOR)
        snapshot = wire.rig.path.read_bytes()  # a backup taken while ORIGIN was allowed
        store.remove(removed.id, ACTOR)
        store.request(NewCallbackEntry("https://later.example.com"), ACTOR)
        wire.dns.answer("later.example.com", [PUBLIC_IP])
        wire.rig.path.write_bytes(snapshot)  # the attacker restores it
        pending = await wire.event()

        policy = wire.policy()
        reasons = {_refused(policy, URL), _refused(policy, "https://later.example.com/cb")}
        listed = await deployment.client.get(CALLBACKS, headers=deployment.operator)
        intake = await deployment.intake(URL)
        await wire.dispatcher().dispatch_once()

        assert reasons == {"allowlist_unavailable"}
        assert listed.status_code == 503, listed.text
        assert intake.status_code == 400, intake.text
        assert await deployment.tasks() == []
        assert wire.receiver.requests == []
        assert wire.audit.outcomes(pending) == [("retry", "allowlist_unavailable")]


# ======================================================================================
# An app principal can never manage the allowlist
# ======================================================================================


class TestAppCannotManageTheAllowlist:
    @pytest.mark.parametrize(
        ("method", "suffix", "body"),
        [
            ("GET", "", None),
            ("POST", "", {"origin": "https://evil.test", "allow_private": True}),
            ("POST", "/{id}/remove", None),
        ],
    )
    async def test_app_with_every_scope_is_refused_with_one_row(
        self,
        deployment: Deployment,
        wire: Wire,
        method: str,
        suffix: str,
        body: dict[str, Any] | None,
    ) -> None:
        entry_id = wire.allow()
        app = issue_principal(deployment.principals.open(), scopes=all_scopes())
        before = len(deployment.refusals())

        resp = await deployment.client.request(
            method,
            CALLBACKS + suffix.format(id=entry_id),
            json=body,
            headers=app.bearer_headers(),
        )

        assert resp.status_code == 403, resp.text
        assert len(deployment.refusals()) - before == 1
        assert [(e.origin, e.status) for e in wire.rig.open().list()] == [(ORIGIN, "active")]

    async def test_app_scopes_never_include_an_allowlist_scope(self) -> None:
        assert not [s for s in Scope if "callback" in s.value or "allowlist" in s.value]
