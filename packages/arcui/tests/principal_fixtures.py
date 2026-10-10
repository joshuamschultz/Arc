"""Shared SPEC-087 app-principal fixtures for arcui tests (T-1277/1278/1281 and later).

Real collaborators wherever possible:

- ``PrincipalStore`` is the production store over a real ``FileJournalAnchor``
  (signed by an ``InProcessSigner``) on the real filesystem. Only the at-rest
  cipher (Vault custody in production) and the audit sinks are doubles; the
  cipher is an authenticated NaCl ``SecretBox`` so tampering is detected.
- Credentials are minted with ``arctrust.principal_auth`` (``issue_secret``,
  ``format_bearer``, ``sign_request``) — never hand-rolled.
- Agent DIDs and app DIDs are derived from real Ed25519 keys.
- The fleet roster is a real ``team/<name>_agent/arcagent.toml`` tree, so the
  route-table agent resolver goes through the production roster provider.

``principal_auth`` is imported lazily inside the helpers so the store and app
fixtures stay usable (and smoke-testable) while that module is being built.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arctrust import FileJournalAnchor, InProcessSigner
from arctrust.audit import AuditEvent
from arctrust.identity import did_from_public_key
from arctrust.keypair import KeyPair, generate_keypair
from arctrust.principals import Principal, PrincipalStore, Scope, derive_app_did
from nacl.secret import SecretBox

ORG = "default"
STORE_ACTOR = "did:arc:test:operator/principal-store-actor"
APPROVER = "did:arc:test:user/principal-approver"
WILDCARD = "*"
_ANCHOR_SIGNER = InProcessSigner(bytes(range(32)))


# --- external-boundary doubles -------------------------------------------------


class SecretBoxCipher:
    """Authenticated ByteCipher standing in for Vault custody."""

    def __init__(self) -> None:
        self._box = SecretBox(bytes(range(32)))

    def seal(self, payload: bytes) -> str:
        return base64.b64encode(bytes(self._box.encrypt(payload))).decode("ascii")

    def open(self, sealed: str) -> bytes:
        return self._box.decrypt(base64.b64decode(sealed, validate=True))


@dataclass
class RecordingSink:
    events: list[AuditEvent] = field(default_factory=list)

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass
class DurableSink:
    events: list[AuditEvent] = field(default_factory=list)

    def write_durable(self, event: AuditEvent) -> None:
        self.events.append(event)


class SpyWorm:
    """Stands in for ``app.state.audit_worm``: records every mutation/refusal row."""

    def __init__(self) -> None:
        self.written: list[Any] = []

    def write(self, fields: Any) -> None:
        self.written.append(fields)

    def denied(self) -> list[Any]:
        return [row for row in self.written if row.outcome == "denied"]


# --- principal store -------------------------------------------------------------


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


@dataclass
class PrincipalRig:
    """One sealed principal authority on disk; ``factory`` reopens it on every call."""

    path: Path
    anchor: FileJournalAnchor
    cipher: SecretBoxCipher = field(default_factory=SecretBoxCipher)
    audit: RecordingSink = field(default_factory=RecordingSink)
    strict: DurableSink = field(default_factory=DurableSink)

    def open(self) -> PrincipalStore:
        return PrincipalStore(
            self.path,
            anchor=self.anchor,
            cipher=self.cipher,
            audit_sink=self.audit,
            strict_audit_sink=self.strict,
            actor_did=STORE_ACTOR,
        )

    @property
    def factory(self) -> Callable[[], PrincipalStore]:
        return self.open


def principal_rig(state_dir: Path) -> PrincipalRig:
    """A real ``PrincipalStore`` authority rooted under ``state_dir``."""
    return PrincipalRig(
        path=_private_dir(state_dir / "principals") / "principals.json",
        anchor=FileJournalAnchor(
            _private_dir(state_dir / "principals-anchor"),
            scope="test/principals",
            signer=_ANCHOR_SIGNER,
        ),
    )


# --- identities ------------------------------------------------------------------


def agent_did() -> str:
    """A real executor DID (contains '/', so it can never be a URL path segment)."""
    return did_from_public_key(generate_keypair().public_key, org=ORG, agent_type="executor")


def all_scopes(agents: tuple[str, ...] = (WILDCARD,)) -> dict[Scope, tuple[str, ...]]:
    """Every catalogue scope bound to ``agents`` ('*' = any agent, personal only)."""
    return {scope: agents for scope in Scope}


@dataclass(frozen=True)
class IssuedPrincipal:
    """An active principal plus the credentials only its app would hold."""

    record: Principal
    bearer: str
    keys: KeyPair

    @property
    def signing_key(self) -> bytes:
        return self.keys.private_key

    @property
    def did(self) -> str:
        return self.record.did

    def bearer_headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.bearer}", **extra}


def issue_principal(
    store: PrincipalStore,
    *,
    scopes: Mapping[Scope, tuple[str, ...]],
    subjects: tuple[str, ...] = (WILDCARD,),
    name: str | None = None,
    expires_in: timedelta | None = timedelta(days=7),
) -> IssuedPrincipal:
    """Request + activate a principal through the real store; return its credentials."""
    from arctrust.principal_auth import format_bearer, issue_secret

    keys = generate_keypair()
    secret, digest = issue_secret()
    now = datetime.now(UTC)
    pending = Principal(
        id=uuid.uuid4().hex,
        name=name or f"app-{uuid.uuid4().hex[:8]}",
        did=derive_app_did(keys.public_key, org=ORG),
        public_key=keys.public_key.hex(),
        secret_digest=digest,
        scopes=dict(scopes),
        subjects=subjects,
        status="pending",
        created_by=STORE_ACTOR,
        approved_by=None,
        created_at=now - timedelta(minutes=5),
        expires_at=None if expires_in is None else now + expires_in,
        last_used_at=None,
        revoked_by=None,
        revoked_at=None,
    )
    store.request(pending)
    active = store.activate(pending.id, APPROVER, pending.content_hash())
    return IssuedPrincipal(record=active, bearer=format_bearer(active.id, secret), keys=keys)


def signed_headers(
    issued: IssuedPrincipal,
    *,
    method: str,
    path: str,
    query: str = "",
    body: bytes = b"",
    on_behalf_of: str | None = None,
) -> dict[str, str]:
    """Headers for a signed principal request (the one Arc request-signing envelope)."""
    from arctrust.principal_auth import sign_request

    return sign_request(
        issued.signing_key,
        principal_did=issued.did,
        method=method,
        path=path,
        query=query,
        body=body,
        on_behalf_of=on_behalf_of,
    )


# --- fleet roster ----------------------------------------------------------------


def write_team(team_root: Path, agents: Mapping[str, str]) -> Path:
    """Write ``<label>_agent/arcagent.toml`` per ``{label: did}`` for the roster provider."""
    team_root.mkdir(parents=True, exist_ok=True)
    for label, did in agents.items():
        agent_dir = team_root / f"{label}_agent"
        (agent_dir / "workspace").mkdir(parents=True, exist_ok=True)
        (agent_dir / "arcagent.toml").write_text(
            f'[agent]\nname = "{label}"\norg = "{ORG}"\ntype = "executor"\n'
            f'[identity]\ndid = "{did}"\n',
            encoding="utf-8",
        )
    return team_root


# --- the app under test ----------------------------------------------------------

OPERATOR_TOKEN = "o" * 64
VIEWER_TOKEN = "v" * 64
PASSWORD = "correct-horse-battery"


@dataclass
class PrincipalApp:
    """A full ``create_app`` build with the principal seam wired, plus its spies."""

    app: Any
    auth: Any
    worm: SpyWorm
    users: Callable[[], Any]
    probes: list[dict[str, Any]] = field(default_factory=list)

    def sign_in(self, email: str, roles: tuple[str, ...]) -> tuple[str, str]:
        """Create an account and a live session; return (session token, account DID)."""
        user = self.users().add(email, PASSWORD, roles=roles)
        role = "operator" if user.is_operator else "viewer"
        session = self.auth.sessions.issue(email=user.email, did=user.did, role=role)
        return session.token, user.did

    def add_probe(
        self,
        path: str,
        methods: tuple[str, ...] = ("POST",),
        handler: Callable[[Any], Any] | None = None,
    ) -> None:
        """Mount a test-only route AHEAD of the real one at ``path``.

        It proves the middleware let the request through (``probes`` grows) and
        reports what the middleware bound: the principal, the on-behalf-of person,
        the causal context, and the body the route could still read.
        """
        from starlette.routing import Route

        self.app.router.routes.insert(
            0, Route(path, handler or self._probe_handler, methods=list(methods))
        )

    async def _probe_handler(self, request: Any) -> Any:
        from arctrust import causal
        from starlette.responses import JSONResponse

        body = await request.body()
        principal = getattr(request.state, "principal", None)
        event = AuditEvent(actor_did=causal.actor_did(), action="probe", target="t", outcome="ok")
        seen = {
            "path": request.url.path,
            "body": body.decode("utf-8", "replace"),
            "principal_did": getattr(principal, "did", None),
            "principal_kind": getattr(principal, "kind", None),
            "principal_on_behalf_of": getattr(principal, "on_behalf_of", None),
            "state_on_behalf_of": getattr(request.state, "on_behalf_of", None),
            "role": getattr(request.state, "role", None),
            "causal": None if event.causal is None else event.causal.model_dump(),
        }
        self.probes.append(seen)
        return JSONResponse({"probe": True, **seen})


def build_principal_app(
    tmp_path: Path,
    *,
    principal_store_factory: Callable[[], PrincipalStore] | None,
    users: Callable[[], Any],
    tier: str = "personal",
    team_root: Path | None = None,
    **config: Any,
) -> PrincipalApp:
    """``create_app`` with the SPEC-087 principal seam and a spy audit WORM."""
    from arcui.principals import PrincipalsConfig

    return _assemble(
        tmp_path,
        users=users,
        team_root=team_root,
        principal_store_factory=principal_store_factory,
        principals_config=PrincipalsConfig(tier=tier, **config),
    )


def build_session_only_app(
    tmp_path: Path, *, users: Callable[[], Any], team_root: Path | None = None
) -> PrincipalApp:
    """The same build WITHOUT the new seam: proves the fixtures themselves work."""
    return _assemble(tmp_path, users=users, team_root=team_root)


def _assemble(
    tmp_path: Path,
    *,
    users: Callable[[], Any],
    team_root: Path | None,
    **seam: Any,
) -> PrincipalApp:
    from arcui.auth import AuthConfig
    from arcui.server import create_app

    auth = AuthConfig({"operator_token": OPERATOR_TOKEN, "viewer_token": VIEWER_TOKEN})
    app = create_app(
        auth_config=auth,
        user_store_factory=users,
        team_root=team_root,
        data_dir=tmp_path / "arcui-data",
        **seam,
    )
    worm = SpyWorm()
    app.state.audit_worm = worm
    return PrincipalApp(app=app, auth=auth, worm=worm, users=users)
