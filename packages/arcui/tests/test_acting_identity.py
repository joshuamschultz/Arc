"""SPEC-087 COMP-006 (T-1279): who is acting, and may they decide?

``arcui.acting.acting_identity(request)`` turns the auth layer's
``request.state.principal`` into one ``ActingIdentity`` that every route and
audit row uses. ``decider(request, scope)`` adds the decision rules for gates,
human_input, task review and approvals (REQ-563, REQ-571).
``is_control_allowed`` replaces the bare operator checks on app-reachable
routes. ``arcui.principals.PrincipalsConfig`` carries the tier switches.

Requirements: REQ-558, REQ-562 (intersection: an app adds no role to the person
it acts for), REQ-563 (federal app decisions need a live delegation the person
made), REQ-571 (static operator token decisions; README "Rollout").

Unit tests: each case builds a real Starlette ``Request`` whose state is shaped
exactly as ``AuthMiddleware`` leaves it. The account authority is real: a
``UserStore`` over test custody fakes and a ``PrincipalStore`` over a real
signed ``FileJournalAnchor``, so delegations are created, expired and revoked in
the production store. Only the audit sinks are doubles (they record rows).
"""

from __future__ import annotations

import base64
import enum
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from arctrust import FileJournalAnchor, InProcessSigner
from arctrust.audit import AuditEvent
from arctrust.principals import Delegation, Principal, PrincipalStore, Scope, derive_app_did
from arctrust.users import OPERATOR, VIEWER, UserStore
from nacl.secret import SecretBox
from nacl.signing import SigningKey
from packages.arcui.tests.user_authority import user_store_factory
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request

from arcui.acting import ActingIdentity, Refusal, acting_identity, decider, is_control_allowed
from arcui.principals import (
    ENTERPRISE_REQUIRE_NAMED_DECIDER_DEFAULT,
    PrincipalsConfig,
    RequestPrincipal,
)

TIERS = ("personal", "enterprise", "federal")
AGENT_A = "did:arc:test:agent/olivia"
AGENT_B = "did:arc:test:agent/sales"
OPERATOR_SESSION_ID = "sess-operator"
VIEWER_SESSION_ID = "sess-viewer"
TOKEN_SESSION_ID = "sess-token"
APP_SESSION_ID = "sess-app"
_STORE_ACTOR = "did:arc:test:operator/principal-store"
_APPROVER = "did:arc:test:user/approver"
_ANCHOR_SIGNER = InProcessSigner(bytes(range(32)))
_DECISION_OPERATION = "operator_token_decision"


# --- external-boundary doubles ----------------------------------------------------


class _SecretBoxCipher:
    """Authenticated cipher standing in for Vault custody of the principal record."""

    def __init__(self) -> None:
        self._box = SecretBox(bytes(range(32)))

    def seal(self, payload: bytes) -> str:
        return base64.b64encode(bytes(self._box.encrypt(payload))).decode("ascii")

    def open(self, sealed: str) -> bytes:
        return self._box.decrypt(base64.b64decode(sealed, validate=True))


@dataclass
class _EventSink:
    events: list[AuditEvent] = field(default_factory=list)

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def write_durable(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass
class _SpyWorm:
    """Stands in for ``app.state.audit_worm``: records every mutation row."""

    written: list[Any] = field(default_factory=list)

    def write(self, fields: Any) -> None:
        self.written.append(fields)


@dataclass
class _SpyAudit:
    """Stands in for ``app.state.audit`` (``UIAuditLogger``): records every event."""

    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def audit_event(self, event: str, details: dict[str, Any]) -> None:
        self.events.append((str(event), dict(details)))


# --- the account authority --------------------------------------------------------


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _principal_store_factory(root: Path) -> Callable[[], PrincipalStore]:
    """A factory that reopens one real, anchored PrincipalStore on every call."""
    state_dir = _private_dir(root / "principals")
    anchor = FileJournalAnchor(
        _private_dir(root / "anchor"), scope="test/principals", signer=_ANCHOR_SIGNER
    )
    cipher = _SecretBoxCipher()
    sink = _EventSink()

    def factory() -> PrincipalStore:
        return PrincipalStore(
            state_dir / "principals.json",
            anchor=anchor,
            cipher=cipher,
            audit_sink=sink,
            strict_audit_sink=sink,
            actor_did=_STORE_ACTOR,
        )

    return factory


def _app_principal(
    scopes: Mapping[Scope, tuple[str, ...]], subjects: tuple[str, ...]
) -> Principal:
    public_key = bytes(SigningKey.generate().verify_key)
    now = datetime.now(UTC)
    return Principal(
        id=uuid.uuid4().hex,
        name=f"app-{uuid.uuid4().hex[:6]}",
        did=derive_app_did(public_key, org="test"),
        public_key=public_key.hex(),
        secret_digest=uuid.uuid4().hex + uuid.uuid4().hex,
        scopes=dict(scopes),
        subjects=subjects,
        status="pending",
        created_by=_STORE_ACTOR,
        approved_by=None,
        created_at=now,
        expires_at=now + timedelta(days=20),
        last_used_at=None,
        revoked_by=None,
        revoked_at=None,
    )


@dataclass
class World:
    """Two named people, two registered apps and the stores that hold them."""

    users: Callable[[], UserStore]
    principals: Callable[[], PrincipalStore]
    operator_did: str
    viewer_did: str
    app: Principal
    other_app: Principal

    def delegate(
        self,
        person_did: str,
        *,
        app_did: str | None = None,
        scopes: tuple[Scope, ...] = (Scope.GATES_RESOLVE,),
        created_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> Delegation:
        """Create a delegation in the real store; live for an hour by default."""
        now = datetime.now(UTC)
        delegation = Delegation(
            id=uuid.uuid4().hex,
            person_did=person_did,
            principal_did=app_did or self.app.did,
            scopes=scopes,
            created_at=created_at or now - timedelta(minutes=1),
            expires_at=expires_at or now + timedelta(hours=1),
            revoked=False,
        )
        return self.principals().add_delegation(delegation)


@dataclass(frozen=True)
class People:
    config_dir: Path
    users: Callable[[], UserStore]
    operator_did: str
    viewer_did: str


@pytest.fixture(scope="module")
def people(tmp_path_factory: pytest.TempPathFactory) -> People:
    """The account store is only read here, so it is built once per module
    (password hashing dominates setup time); principal state is per test."""
    root = tmp_path_factory.mktemp("acting-people")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("ARC_CONFIG_DIR", str(root))
        users = user_store_factory(root / "accounts")
        store = users()
        operator = store.add("boss@example.com", "correct-horse-battery", roles=(OPERATOR,))
        viewer = store.add("watcher@example.com", "correct-horse-battery", roles=(VIEWER,))
    return People(root, users, operator.did, viewer.did)


@pytest.fixture
def world(people: People, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(people.config_dir))
    principals = _principal_store_factory(tmp_path / "authority")
    grants = {Scope.GATES_RESOLVE: (AGENT_A,), Scope.TASKS_WRITE: (AGENT_A,)}
    subjects = (people.operator_did, people.viewer_did)
    app = principals().request(_app_principal(grants, subjects))
    other = principals().request(_app_principal(grants, subjects))
    return World(
        users=people.users,
        principals=principals,
        operator_did=people.operator_did,
        viewer_did=people.viewer_did,
        app=app,
        other_app=other,
    )


# --- requests shaped as AuthMiddleware leaves them ----------------------------------


class Caller(enum.Enum):
    SESSION_OPERATOR = "session operator"
    SESSION_VIEWER = "session viewer"
    TOKEN_OPERATOR = "static operator token"
    TOKEN_VIEWER = "static viewer token"
    APP_ALONE = "app alone"
    APP_FOR_OPERATOR = "app for operator person"
    APP_FOR_VIEWER = "app for viewer person"


IDENTITY_CALLERS = (
    Caller.SESSION_OPERATOR,
    Caller.SESSION_VIEWER,
    Caller.TOKEN_OPERATOR,
    Caller.APP_ALONE,
    Caller.APP_FOR_OPERATOR,
    Caller.APP_FOR_VIEWER,
)

_DEFAULT_APP_SCOPES: Mapping[Scope, frozenset[str]] = {
    Scope.GATES_RESOLVE: frozenset({AGENT_A}),
    Scope.TASKS_WRITE: frozenset({AGENT_A}),
}


@dataclass
class Built:
    request: Request
    worm: _SpyWorm
    audit: _SpyAudit

    def rows(self, operation: str) -> list[Any]:
        return [f for f in self.worm.written if f.operation == operation]

    def logged(self, operation: str) -> list[dict[str, Any]]:
        return [d for _, d in self.audit.events if d.get("operation") == operation]


def _token_pseudo_did(session_id: str) -> str:
    return f"did:arc:ui:session:{session_id}"


def _state_for(
    world: World,
    caller: Caller,
    app_scopes: Mapping[Scope, frozenset[str]],
    app_did: str,
) -> dict[str, Any]:
    """``request.state`` fields exactly as AuthMiddleware sets them per caller."""
    if caller is Caller.SESSION_OPERATOR:
        return _session_state(world.operator_did, OPERATOR, OPERATOR_SESSION_ID)
    if caller is Caller.SESSION_VIEWER:
        return _session_state(world.viewer_did, VIEWER, VIEWER_SESSION_ID)
    if caller is Caller.TOKEN_OPERATOR:
        return _token_state(OPERATOR)
    if caller is Caller.TOKEN_VIEWER:
        return _token_state(VIEWER)
    if caller is Caller.APP_ALONE:
        return _app_state(app_did, app_scopes, None, frozenset())
    if caller is Caller.APP_FOR_OPERATOR:
        return _app_state(app_did, app_scopes, world.operator_did, frozenset({OPERATOR}))
    return _app_state(app_did, app_scopes, world.viewer_did, frozenset({VIEWER}))


def _session_state(did: str, role: str, session_id: str) -> dict[str, Any]:
    roles = frozenset({role})
    return {
        "role": role,
        "account_did": did,
        "session_id": session_id,
        "on_behalf_of": None,
        "principal": RequestPrincipal(
            did=did, kind="user", scopes={}, on_behalf_of=None, roles=roles
        ),
    }


def _token_state(role: str) -> dict[str, Any]:
    return {
        "role": role,
        "account_did": None,
        "session_id": TOKEN_SESSION_ID,
        "on_behalf_of": None,
        "principal": RequestPrincipal(
            did=_token_pseudo_did(TOKEN_SESSION_ID),
            kind="operator",
            scopes={},
            on_behalf_of=None,
            roles=frozenset({role}),
        ),
    }


def _app_state(
    app_did: str,
    scopes: Mapping[Scope, frozenset[str]],
    person_did: str | None,
    person_roles: frozenset[str],
) -> dict[str, Any]:
    # The middleware hands over the person's roles (if any); an app has none.
    return {
        "role": "app",
        "account_did": None,
        "session_id": APP_SESSION_ID,
        "on_behalf_of": person_did,
        "principal": RequestPrincipal(
            did=app_did,
            kind="app",
            scopes=dict(scopes),
            on_behalf_of=person_did,
            roles=person_roles,
        ),
    }


def build_request(
    world: World,
    caller: Caller,
    config: PrincipalsConfig,
    *,
    app_scopes: Mapping[Scope, frozenset[str]] = _DEFAULT_APP_SCOPES,
    app_did: str | None = None,
    principal_store_factory: Callable[[], PrincipalStore] | None | str = "real",
) -> Built:
    app = Starlette()
    worm, audit = _SpyWorm(), _SpyAudit()
    app.state.principals_config = config
    app.state.user_store_factory = world.users
    app.state.principal_store_factory = (
        world.principals if principal_store_factory == "real" else principal_store_factory
    )
    app.state.audit = audit
    app.state.audit_worm = worm
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": f"/api/workflow-tasks/{uuid.uuid4().hex}/gate",
            "raw_path": b"/api/workflow-tasks/x/gate",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )
    for key, value in _state_for(world, caller, app_scopes, app_did or world.app.did).items():
        setattr(request.state, key, value)
    return Built(request=request, worm=worm, audit=audit)


def _config(tier: str, **kwargs: Any) -> PrincipalsConfig:
    return PrincipalsConfig(tier=tier, **kwargs)


# --- acting_identity: the identity table ---------------------------------------------


@dataclass(frozen=True)
class Expected:
    actor_did: str
    via_app: str | None
    on_behalf_of: str | None
    roles: frozenset[str]
    named_person: bool
    session_id: str | None  # None: not pinned for this caller


def _expected(world: World, caller: Caller) -> Expected:
    app = world.app.did
    table = {
        Caller.SESSION_OPERATOR: Expected(
            world.operator_did, None, None, frozenset({OPERATOR}), True, OPERATOR_SESSION_ID
        ),
        Caller.SESSION_VIEWER: Expected(
            world.viewer_did, None, None, frozenset({VIEWER}), True, VIEWER_SESSION_ID
        ),
        Caller.TOKEN_OPERATOR: Expected(
            _token_pseudo_did(TOKEN_SESSION_ID),
            None,
            None,
            frozenset({OPERATOR}),
            False,
            TOKEN_SESSION_ID,
        ),
        # An app alone names nobody: it acts as itself with no role at all.
        Caller.APP_ALONE: Expected(app, None, None, frozenset(), False, None),
        Caller.APP_FOR_OPERATOR: Expected(
            world.operator_did, app, world.operator_did, frozenset({OPERATOR}), True, None
        ),
        Caller.APP_FOR_VIEWER: Expected(
            world.viewer_did, app, world.viewer_did, frozenset({VIEWER}), True, None
        ),
    }
    return table[caller]


@pytest.mark.parametrize("tier", TIERS)
@pytest.mark.parametrize("caller", IDENTITY_CALLERS, ids=lambda c: c.value)
def test_acting_identity_table_names_actor_app_person_and_roles(
    world: World, caller: Caller, tier: str
) -> None:
    built = build_request(world, caller, _config(tier))
    expected = _expected(world, caller)

    identity = acting_identity(built.request)

    assert isinstance(identity, ActingIdentity)
    assert identity.actor_did == expected.actor_did
    assert identity.via_app == expected.via_app
    assert identity.on_behalf_of == expected.on_behalf_of
    assert identity.roles == expected.roles
    assert identity.named_person is expected.named_person
    if expected.session_id is not None:
        assert identity.session_id == expected.session_id


@pytest.mark.parametrize("tier", TIERS)
def test_acting_identity_app_for_viewer_person_never_gains_operator(
    world: World, tier: str
) -> None:
    # REQ-562: the app holds gates:resolve and tasks:write, but the person is a
    # viewer; the acting identity is the intersection, so no operator role.
    built = build_request(world, Caller.APP_FOR_VIEWER, _config(tier))

    identity = acting_identity(built.request)

    assert OPERATOR not in identity.roles
    assert "app" not in identity.roles


def test_acting_identity_is_immutable(world: World) -> None:
    identity = acting_identity(
        build_request(world, Caller.SESSION_OPERATOR, _config("federal")).request
    )
    with pytest.raises((AttributeError, TypeError)):
        identity.actor_did = "did:arc:attacker"  # type: ignore[misc]  # reason: proving the dataclass is frozen


def test_acting_identity_two_apps_for_one_person_record_different_via_app(world: World) -> None:
    first = acting_identity(
        build_request(world, Caller.APP_FOR_OPERATOR, _config("enterprise")).request
    )
    second = acting_identity(
        build_request(
            world, Caller.APP_FOR_OPERATOR, _config("enterprise"), app_did=world.other_app.did
        ).request
    )
    assert first.actor_did == second.actor_did == world.operator_did
    assert (first.via_app, second.via_app) == (world.app.did, world.other_app.did)


# --- decider: static operator token (REQ-571) ------------------------------------------


def test_decider_federal_static_token_is_refused_operator_account_required(world: World) -> None:
    built = build_request(world, Caller.TOKEN_OPERATOR, _config("federal"))

    result = decider(built.request, Scope.GATES_RESOLVE)

    assert result == Refusal(code="operator_account_required", status=403)
    assert built.rows(_DECISION_OPERATION) == []


@pytest.mark.parametrize("setting", [None, False, True])
def test_decider_federal_static_token_refused_whatever_the_setting(
    world: World, setting: bool | None
) -> None:
    built = build_request(
        world, Caller.TOKEN_OPERATOR, _config("federal", require_named_decider=setting)
    )
    assert decider(built.request, Scope.GATES_RESOLVE) == Refusal(
        code="operator_account_required", status=403
    )


def test_decider_enterprise_static_token_refused_when_named_decider_required(
    world: World,
) -> None:
    built = build_request(
        world, Caller.TOKEN_OPERATOR, _config("enterprise", require_named_decider=True)
    )

    result = decider(built.request, Scope.GATES_RESOLVE)

    assert result == Refusal(code="operator_account_required", status=403)
    assert built.rows(_DECISION_OPERATION) == []


@pytest.mark.parametrize("setting", [None, False], ids=["unset", "false"])
def test_decider_enterprise_static_token_allowed_with_one_warning_row(
    world: World, setting: bool | None
) -> None:
    built = build_request(
        world, Caller.TOKEN_OPERATOR, _config("enterprise", require_named_decider=setting)
    )

    result = decider(built.request, Scope.GATES_RESOLVE)

    assert isinstance(result, ActingIdentity)
    assert result.actor_did == _token_pseudo_did(TOKEN_SESSION_ID)
    assert result.named_person is False
    (row,) = built.rows(_DECISION_OPERATION)
    assert row.actor_did == _token_pseudo_did(TOKEN_SESSION_ID)
    assert len(built.logged(_DECISION_OPERATION)) == 1


@pytest.mark.parametrize("setting", [None, False, True], ids=["unset", "false", "true"])
def test_decider_personal_static_token_ignores_setting_and_writes_no_warning(
    world: World, setting: bool | None
) -> None:
    built = build_request(
        world, Caller.TOKEN_OPERATOR, _config("personal", require_named_decider=setting)
    )

    result = decider(built.request, Scope.GATES_RESOLVE)

    assert isinstance(result, ActingIdentity)
    assert result.actor_did == _token_pseudo_did(TOKEN_SESSION_ID)
    assert built.rows(_DECISION_OPERATION) == []
    assert built.logged(_DECISION_OPERATION) == []


@pytest.mark.parametrize("tier", TIERS)
@pytest.mark.parametrize("setting", [None, False, True], ids=["unset", "false", "true"])
def test_decider_named_session_operator_always_allowed_without_warning(
    world: World, tier: str, setting: bool | None
) -> None:
    built = build_request(
        world, Caller.SESSION_OPERATOR, _config(tier, require_named_decider=setting)
    )

    result = decider(built.request, Scope.GATES_RESOLVE)

    assert isinstance(result, ActingIdentity)
    assert result.actor_did == world.operator_did
    assert result.named_person is True
    assert built.rows(_DECISION_OPERATION) == []


def test_decider_federal_app_alone_cannot_decide(world: World) -> None:
    # No person behind the call: a decision has nobody to record. The code is
    # not pinned by the SDD for this case; the refusal and its 403 are.
    built = build_request(world, Caller.APP_ALONE, _config("federal"))

    result = decider(built.request, Scope.GATES_RESOLVE)

    assert isinstance(result, Refusal)
    assert result.status == 403


# --- decider: app acting for a person (REQ-563) -----------------------------------------


@pytest.mark.parametrize("scope", [Scope.GATES_RESOLVE, Scope.TASKS_WRITE])
def test_decider_federal_app_for_person_without_delegation_is_refused(
    world: World, scope: Scope
) -> None:
    built = build_request(world, Caller.APP_FOR_OPERATOR, _config("federal"))

    assert decider(built.request, scope) == Refusal(code="delegation_required", status=403)


def test_decider_federal_app_for_person_with_live_delegation_is_allowed(world: World) -> None:
    world.delegate(world.operator_did)
    built = build_request(world, Caller.APP_FOR_OPERATOR, _config("federal"))

    result = decider(built.request, Scope.GATES_RESOLVE)

    assert isinstance(result, ActingIdentity)
    assert result.actor_did == world.operator_did
    assert result.on_behalf_of == world.operator_did
    assert result.via_app == world.app.did
    assert built.rows(_DECISION_OPERATION) == []


def test_decider_federal_expired_delegation_is_refused(world: World) -> None:
    now = datetime.now(UTC)
    world.delegate(
        world.operator_did,
        created_at=now - timedelta(hours=9),
        expires_at=now - timedelta(hours=1),
    )
    built = build_request(world, Caller.APP_FOR_OPERATOR, _config("federal"))

    assert decider(built.request, Scope.GATES_RESOLVE) == Refusal(
        code="delegation_required", status=403
    )


def test_decider_federal_revoked_delegation_is_refused(world: World) -> None:
    delegation = world.delegate(world.operator_did)
    built = build_request(world, Caller.APP_FOR_OPERATOR, _config("federal"))
    assert isinstance(decider(built.request, Scope.GATES_RESOLVE), ActingIdentity)

    world.principals().revoke_delegation(delegation.id, world.operator_did)

    assert decider(built.request, Scope.GATES_RESOLVE) == Refusal(
        code="delegation_required", status=403
    )


def test_decider_federal_delegation_for_another_scope_does_not_cover(world: World) -> None:
    world.delegate(world.operator_did, scopes=(Scope.TASKS_WRITE,))
    built = build_request(world, Caller.APP_FOR_OPERATOR, _config("federal"))

    assert decider(built.request, Scope.GATES_RESOLVE) == Refusal(
        code="delegation_required", status=403
    )


def test_decider_federal_delegation_to_another_app_does_not_cover(world: World) -> None:
    # One app must not ride on the delegation a person gave to a different app.
    world.delegate(world.operator_did, app_did=world.other_app.did)
    built = build_request(world, Caller.APP_FOR_OPERATOR, _config("federal"))

    assert decider(built.request, Scope.GATES_RESOLVE) == Refusal(
        code="delegation_required", status=403
    )


def test_decider_federal_delegation_from_another_person_does_not_cover(world: World) -> None:
    # The viewer delegated to this app; that says nothing about the operator.
    world.delegate(world.viewer_did)
    built = build_request(world, Caller.APP_FOR_OPERATOR, _config("federal"))

    assert decider(built.request, Scope.GATES_RESOLVE) == Refusal(
        code="delegation_required", status=403
    )


@pytest.mark.parametrize("tier", ["personal", "enterprise"])
def test_decider_non_federal_app_for_person_needs_no_delegation(world: World, tier: str) -> None:
    built = build_request(world, Caller.APP_FOR_OPERATOR, _config(tier))

    result = decider(built.request, Scope.GATES_RESOLVE)

    assert isinstance(result, ActingIdentity)
    assert result.actor_did == world.operator_did
    assert result.via_app == world.app.did
    assert built.rows(_DECISION_OPERATION) == []


def _raising_factory() -> PrincipalStore:
    raise OSError("account authority unreachable")


@pytest.mark.parametrize("factory", [None, _raising_factory], ids=["absent", "raising"])
def test_decider_federal_app_fails_closed_when_authority_unavailable(
    world: World, factory: Callable[[], PrincipalStore] | None
) -> None:
    # A live delegation exists, but the decider cannot see it: refuse, never allow.
    world.delegate(world.operator_did)
    built = build_request(
        world, Caller.APP_FOR_OPERATOR, _config("federal"), principal_store_factory=factory
    )

    result = decider(built.request, Scope.GATES_RESOLVE)

    assert isinstance(result, Refusal)
    assert result.status in (403, 503)


# --- PrincipalsConfig pins --------------------------------------------------------------


def test_enterprise_require_named_decider_default_is_false_this_release() -> None:
    """Pins the enterprise default for this release (SPEC-087 README "Rollout").

    The next release flips ``ENTERPRISE_REQUIRE_NAMED_DECIDER_DEFAULT`` to True
    so enterprise refuses static-token decisions by default. That flip must be a
    deliberate one-line change: edit the constant AND this test, plus a release
    note. No migration, no shim.
    """
    assert ENTERPRISE_REQUIRE_NAMED_DECIDER_DEFAULT is False


def test_enterprise_named_decider_required_defaults_false() -> None:
    assert PrincipalsConfig(tier="enterprise").named_decider_required is False


@pytest.mark.parametrize(
    ("tier", "setting", "required"),
    [
        ("personal", None, False),
        ("personal", False, False),
        ("personal", True, False),
        ("enterprise", None, False),
        ("enterprise", False, False),
        ("enterprise", True, True),
        ("federal", None, True),
        ("federal", False, True),
        ("federal", True, True),
    ],
)
def test_named_decider_required_matrix(tier: str, setting: bool | None, required: bool) -> None:
    config = PrincipalsConfig(tier=tier, require_named_decider=setting)
    assert config.named_decider_required is required


def test_federal_forces_named_decider_even_when_set_false() -> None:
    assert (
        PrincipalsConfig(tier="federal", require_named_decider=False).named_decider_required
        is True
    )


@pytest.mark.parametrize(
    ("tier", "setting", "required"),
    [
        ("personal", False, False),
        ("personal", True, False),
        ("enterprise", False, False),
        ("enterprise", True, True),
        ("federal", False, True),
        ("federal", True, True),
    ],
)
def test_signed_required_matrix(tier: str, setting: bool, required: bool) -> None:
    config = PrincipalsConfig(tier=tier, require_signed_requests=setting)
    assert config.signed_required is required


def test_signed_requests_default_off_below_federal() -> None:
    assert PrincipalsConfig(tier="enterprise").signed_required is False
    assert PrincipalsConfig(tier="federal").signed_required is True


def test_principals_config_rejects_unknown_tier() -> None:
    with pytest.raises(ValidationError):
        PrincipalsConfig(tier="unclassified")  # type: ignore[arg-type]  # reason: proving bad tiers fail closed


def test_principals_config_is_frozen() -> None:
    config = PrincipalsConfig(tier="enterprise")
    with pytest.raises(ValidationError):
        config.require_named_decider = True  # type: ignore[misc]  # reason: proving the model is frozen


# --- is_control_allowed ----------------------------------------------------------------


@pytest.mark.parametrize("tier", TIERS)
@pytest.mark.parametrize(
    ("caller", "allowed"),
    [
        (Caller.SESSION_OPERATOR, True),
        (Caller.TOKEN_OPERATOR, True),
        (Caller.SESSION_VIEWER, False),
        (Caller.TOKEN_VIEWER, False),
    ],
    ids=lambda v: v.value if isinstance(v, Caller) else str(v),
)
def test_is_control_allowed_for_sessions_and_tokens(
    world: World, tier: str, caller: Caller, allowed: bool
) -> None:
    built = build_request(world, caller, _config(tier))
    assert is_control_allowed(built.request, Scope.TASKS_WRITE, AGENT_A) is allowed


@pytest.mark.parametrize("tier", TIERS)
@pytest.mark.parametrize("scope", [Scope.TASKS_WRITE, Scope.GATES_RESOLVE])
@pytest.mark.parametrize("caller", [Caller.APP_ALONE, Caller.APP_FOR_OPERATOR])
def test_is_control_allowed_app_with_scope_bound_to_agent(
    world: World, tier: str, caller: Caller, scope: Scope
) -> None:
    # An app alone is scope-bound only; an app for an operator person passes
    # because the person could do it themselves and the scope is bound.
    built = build_request(world, caller, _config(tier))
    assert is_control_allowed(built.request, scope, AGENT_A) is True


@pytest.mark.parametrize("tier", TIERS)
def test_is_control_allowed_app_for_viewer_person_cannot_write_tasks(
    world: World, tier: str
) -> None:
    # REQ-562 intersection: the app holds tasks:write for AGENT_A, but the
    # viewer it acts for could not write tasks, so neither can the app.
    built = build_request(world, Caller.APP_FOR_VIEWER, _config(tier))
    assert is_control_allowed(built.request, Scope.TASKS_WRITE, AGENT_A) is False


@pytest.mark.parametrize("tier", TIERS)
def test_is_control_allowed_app_for_viewer_person_may_resolve_gates(
    world: World, tier: str
) -> None:
    # gates:resolve is the exception: a viewer listed as a gate approver may
    # resolve that gate; the control plane enforces the approver entries.
    built = build_request(world, Caller.APP_FOR_VIEWER, _config(tier))
    assert is_control_allowed(built.request, Scope.GATES_RESOLVE, AGENT_A) is True


@pytest.mark.parametrize("tier", TIERS)
def test_is_control_allowed_app_for_viewer_person_unbound_gate_agent_is_false(
    world: World, tier: str
) -> None:
    # The viewer exception never widens the app's own agent binding.
    built = build_request(world, Caller.APP_FOR_VIEWER, _config(tier))
    assert is_control_allowed(built.request, Scope.GATES_RESOLVE, AGENT_B) is False


@pytest.mark.parametrize("tier", TIERS)
def test_is_control_allowed_app_scope_bound_to_other_agent_is_false(
    world: World, tier: str
) -> None:
    built = build_request(world, Caller.APP_ALONE, _config(tier))
    assert is_control_allowed(built.request, Scope.TASKS_WRITE, AGENT_B) is False


@pytest.mark.parametrize("tier", TIERS)
def test_is_control_allowed_app_without_the_scope_is_false(world: World, tier: str) -> None:
    built = build_request(
        world,
        Caller.APP_FOR_OPERATOR,
        _config(tier),
        app_scopes={Scope.INBOX_SEND: frozenset({AGENT_A})},
    )
    # Even acting for an operator person, the app holds no tasks:write.
    assert is_control_allowed(built.request, Scope.TASKS_WRITE, AGENT_A) is False


def test_is_control_allowed_app_with_no_scopes_is_false(world: World) -> None:
    built = build_request(world, Caller.APP_ALONE, _config("personal"), app_scopes={})
    assert is_control_allowed(built.request, Scope.TASKS_WRITE, AGENT_A) is False


def test_is_control_allowed_wildcard_binding_covers_any_agent_at_personal(world: World) -> None:
    built = build_request(
        world,
        Caller.APP_ALONE,
        _config("personal"),
        app_scopes={Scope.TASKS_WRITE: frozenset({"*"})},
    )
    assert is_control_allowed(built.request, Scope.TASKS_WRITE, AGENT_B) is True


@pytest.mark.parametrize("tier", ["enterprise", "federal"])
def test_is_control_allowed_wildcard_binding_refused_above_personal(
    world: World, tier: str
) -> None:
    # Defense in depth: validate_for_tier already forbids '*' here, but a stray
    # wildcard must never widen an app's reach at enterprise or federal.
    built = build_request(
        world,
        Caller.APP_ALONE,
        _config(tier),
        app_scopes={Scope.TASKS_WRITE: frozenset({"*"})},
    )
    assert is_control_allowed(built.request, Scope.TASKS_WRITE, AGENT_B) is False
