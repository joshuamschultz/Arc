"""Shared arcui test fixtures.

Isolate every test from the developer's real Arc data dir: the server now runs
an arcstore ``StoreIngest`` (Observe plane, SPEC-026 FR-5) in its lifespan, so
without this an in-process ``TestClient`` would backfill/write into
``~/.arc/store``. Pointing ``ARCSTORE_DATA_DIR`` at a per-test tmp dir keeps the
observability mirror hermetic.

The same reasoning covers the broker: the lifespan hosts the embedded gateway,
which ensures one on startup (COMP-008 / REQ-306), so an unguarded test would
spawn a real ``nats-server`` against the developer's JetStream store.
"""

from __future__ import annotations

import importlib
import inspect
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from arctrust.paths import ARC_CONFIG_DIR_ENV
from arctrust.users import OPERATOR
from starlette.types import ASGIApp, Receive, Send
from starlette.types import Scope as ASGIScope


@pytest.fixture
def arcstore_backend() -> object:
    """Return the per-test in-memory ArcStore backend."""
    from arcstore.backends.memory import FakeBackend

    return FakeBackend()


@pytest.fixture(autouse=True)
def _isolated_arcstore_backend(monkeypatch: pytest.MonkeyPatch, arcstore_backend: object) -> None:
    """Make every implicit ArcUI composition use a fresh contract-complete backend.

    Production requires PostgreSQL and the CLI validates that requirement before
    constructing the app. Unit and route tests that do not explicitly inject a
    backend must remain hermetic rather than inheriting a developer DSN.
    """

    def open_fake(**_kwargs: object) -> object:
        return arcstore_backend

    monkeypatch.setattr("arcstore.backends.open_backend", open_fake)
    monkeypatch.setattr("arcui.observe.open_backend", open_fake)
    monkeypatch.setattr("arcui.server.open_backend", open_fake)


@pytest.fixture(autouse=True)
def _isolated_arc_data_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    data_dir = tmp_path_factory.mktemp("arcdata")
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(data_dir))
    yield data_dir


@pytest.fixture(autouse=True)
def _no_managed_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer as an already-running broker would: no process, no socket, nothing owned.

    Patched at the canonical path AND on the consumer module that binds the name
    at import time — patching one of the two is what lets a real call through.
    """
    import arcteam.nats_server as nats_server

    async def _already_running(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(nats_server, "ensure_nats_server", _already_running)
    consumer = importlib.import_module("arcgateway.broker_bootstrap")
    monkeypatch.setattr(consumer, "ensure_nats_server", _already_running)


@pytest.fixture(autouse=True)
def _isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Relocate the Arc home so no test can reach the developer's real ``~/.arc``.

    Without this, a test that resolves any :mod:`arctrust.paths` accessor writes
    into the invoking user's own deployment: this suite was observed rewriting
    the real viewer token and APPENDING to the real operator-signed WORM chain.
    Both are live state a developer depends on, and the WORM chain additionally
    enforces a single writer, so a polluting test also fails outright beside any
    concurrent run — diagnosing the machine rather than the code.

    Autouse and set before every other fixture body, so a test that names its own
    ``ARC_CONFIG_DIR`` still wins (its ``setenv`` lands after this one).
    """
    monkeypatch.setenv(ARC_CONFIG_DIR_ENV, str(tmp_path / "arc-home"))


@pytest.fixture
async def _stop_sync_worker_in_loop() -> AsyncIterator[None]:
    yield
    from arcagent.modules.connected_data.sync_worker.supervisor import (
        shutdown_process_supervisor,
    )

    await shutdown_process_supervisor()


@pytest.fixture(autouse=True)
def _no_sync_worker_outlives_its_test(request: pytest.FixtureRequest) -> Iterator[None]:
    """A test that wrote a connected store started a sync worker; it dies with the test.

    A coroutine test stops it in order on its own loop; anything left over (a
    supervisor started on a loop that is gone) is killed outright.
    """
    if inspect.iscoroutinefunction(getattr(request, "function", None)):
        request.getfixturevalue("_stop_sync_worker_in_loop")
    yield
    from arcagent.modules.connected_data.sync_worker.supervisor import (
        discard_process_supervisor,
    )

    discard_process_supervisor()


# ---------------------------------------------------------------------------
# SPEC-088 T-1330: put a SPEC-087 RequestPrincipal on a test app's requests
# ---------------------------------------------------------------------------

_TEST_PRINCIPAL = "test_principal"
_TEST_SESSION_ID = "principal-factory-session"


class _BindPrincipal:
    """ASGI middleware: write the bound principal onto ``request.state`` per request.

    Mirrors what ``AuthMiddleware`` leaves behind for each caller kind (principal,
    role, account DID, session id, on-behalf-of), so routes and the audit layer see
    exactly the state the real middleware would hand them. It reads the principal
    from ``app.state`` on every request, so a test can swap callers mid-test.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            principal = getattr(scope["app"].state, _TEST_PRINCIPAL, None)
            if principal is not None:
                scope.setdefault("state", {}).update(_state_for(principal))
        await self.app(scope, receive, send)


def _state_for(principal: Any) -> dict[str, Any]:
    if principal.kind == "app":
        role, account_did = "app", None
    elif principal.kind == "user":
        role = "operator" if OPERATOR in principal.roles else "viewer"
        account_did = principal.did
    else:
        role, account_did = next(iter(principal.roles)), None
    return {
        "principal": principal,
        "role": role,
        "account_did": account_did,
        "session_id": _TEST_SESSION_ID,
        "on_behalf_of": principal.on_behalf_of,
    }


class PrincipalFactory:
    """Builds SPEC-087 principals and binds them to a bare Starlette test app.

    ``install`` wires the seam (``principals_config``, a spy ``audit_worm``, the
    binding middleware) and returns the spy; ``act_as`` chooses who calls next.
    The principals come from the production constructors where they exist.
    """

    def app(
        self,
        scopes: Mapping[Any, Iterable[str]] | None = None,
        *,
        on_behalf_of: str | None = None,
        person_roles: Iterable[str] = (),
        did: str | None = None,
    ) -> Any:
        """An app principal; ``scopes`` maps each Scope to the agent DIDs it is bound to.

        With ``on_behalf_of`` the person's roles ride along, as the middleware
        does after resolving ``X-Arc-On-Behalf-Of``; an app alone has no roles.
        """
        from arcui.principals import RequestPrincipal

        bound = {scope: frozenset(agents) for scope, agents in (scopes or {}).items()}
        return RequestPrincipal(
            did=did or f"did:arc:default:app/{uuid.uuid4().hex[:16]}",
            kind="app",
            scopes=bound,
            on_behalf_of=on_behalf_of,
            roles=frozenset(person_roles) if on_behalf_of else frozenset(),
        )

    def user(self, roles: Iterable[str], *, did: str | None = None) -> Any:
        """A signed-in person (session) with the given account roles."""
        from arcui.principals import session_principal

        return session_principal(
            did or f"did:arc:default:user/{uuid.uuid4().hex[:16]}", tuple(roles)
        )

    def operator_token(self) -> Any:
        """The static break-glass operator token (a session pseudo-DID, no person)."""
        from arcui.principals import static_token_principal

        return static_token_principal(f"did:arc:ui:session:{_TEST_SESSION_ID}", OPERATOR)

    def install(self, app: Any, *, tier: str = "personal") -> Any:
        """Wire the principal seam onto ``app``; return the spy audit WORM."""
        from packages.arcui.tests.principal_fixtures import SpyWorm

        from arcui.principals import PrincipalsConfig

        worm = SpyWorm()
        app.state.principals_config = PrincipalsConfig(tier=tier)
        app.state.audit_worm = worm
        app.add_middleware(_BindPrincipal)
        return worm

    def act_as(self, app: Any, principal: Any) -> None:
        """Every following request to ``app`` carries ``principal``."""
        setattr(app.state, _TEST_PRINCIPAL, principal)


@pytest.fixture
def principal_factory() -> PrincipalFactory:
    """Build app / user / operator-token principals and bind them to a test app."""
    return PrincipalFactory()
