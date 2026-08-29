"""H-021 regression — the Audit page must not 500 against the real backend.

``conftest.py::_isolated_arcstore_backend`` (autouse) points every implicit
arcui composition at the in-memory ``FakeBackend`` so the rest of the suite
stays hermetic. That fake tolerates ordering by ANY column name
(``arcstore.backends.memory.FakeBackend.query`` sorts on whatever key you
hand it), so it never caught that ``Observe.audit()`` and
``Observe.run_recalls()`` requested ``order_by="seq DESC"`` — a value the
production ``PostgresBackend`` never actually supported. Its ``query()``
allowlists only ``ts``-based ordering (``arcstore/backends/postgres.py``
``_ORDER_BY = frozenset({"ts", "ts ASC", "ts DESC"})``) and raises
``ValueError`` for anything else, which propagated out of the unhandled
route handler as an HTTP 500 — exactly the "Audit" dashboard tab failure.

This test builds the app against a REAL ``PostgresBackend`` (the backend
every production deployment actually runs, per
``arcstore.backends.open_backend``) to exercise that path directly instead
of the masking fake. Skips if no ``ARCSTORE_TEST_DATABASE_URL`` is set.

The backend is constructed (never started) OUTSIDE any async fixture and
handed to ``create_app(arcstore_backend=...)`` directly: ``conftest.py``'s
autouse ``_isolated_arcstore_backend`` monkeypatches ``open_backend`` itself
(even when ``arcstore_secret`` is passed, since ``create_app`` calls
``arcstore_backend or open_backend(...)``), so routing around the fake
requires supplying a concrete backend instance, not just a DSN. And it must
not be pre-``start()``ed in a pytest-asyncio fixture: asyncpg pins its pool
to the event loop that created it, while Starlette's ``TestClient`` runs the
app's lifespan on its own portal-thread loop — a pool started on the wrong
loop breaks. ``PostgresBackend.__init__`` is loop-independent (just stores
settings), so the app's own lifespan safely calls ``.start()`` on the
correct loop, exactly like production's ``open_backend()`` path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from arcstore.backends.postgres import PostgresBackend
from arcstore.config import ArcStoreConfig
from pydantic import SecretStr
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app


def _dsn() -> str:
    dsn = os.environ.get("ARCSTORE_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ARCSTORE_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return dsn


def _write_worm_audit(
    data_dir: Path, *, chain_name: str, seq: int, actor_did: str, target: str
) -> None:
    """Append one signed-chain record to a durable WORM file arcstore mirrors.

    ``chain_name`` must be unique per test run: ``StoreIngest`` persists its
    read cursor per filename (``arcstore_cursors`` — keyed on ``path.name``
    alone, not the full path) in the SAME shared Postgres test database every
    run of this test uses, so reusing the production filename
    (``audit-chain.jsonl``) across runs would make a later run's fresh,
    shorter file look "already fully consumed" by an earlier run's cursor.
    """
    worm = data_dir / "worm"
    worm.mkdir(parents=True, exist_ok=True)
    line = {
        "seq": seq,
        "event_hash": f"hash-{target}-{seq}",
        "prev_hash": f"hash-{target}-{seq - 1}" if seq else "",
        "signature": "sig",
        "event": {
            "ts": f"2026-05-31T00:00:{seq:02d}+00:00",
            "actor_did": actor_did,
            "action": "gateway.fs.read",
            "target": target,
            "outcome": "allow",
        },
    }
    with (worm / chain_name).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _viewer(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.viewer_token}"}


def test_audit_route_loads_against_real_postgres_backend(
    _isolated_arc_data_dir: Path,
) -> None:
    """GET /api/team/audit returns 200 (not 500) against production's real backend.

    ``target`` is a random per-test marker so this assertion is unaffected by
    whatever other rows already live in the shared test database's
    ``audit_chain`` table.
    """
    dsn = _dsn()
    marker = uuid4().hex
    target = f"h021-test:{marker}"
    chain_name = f"audit-chain-h021-{marker}.jsonl"
    for i in range(3):
        _write_worm_audit(
            _isolated_arc_data_dir,
            chain_name=chain_name,
            seq=i,
            actor_did=f"did:arc:a{i}",
            target=target,
        )

    backend = PostgresBackend(ArcStoreConfig().postgres_settings(SecretStr(dsn)))
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = create_app(auth_config=auth, arcstore_backend=backend)
    with TestClient(app) as client:
        resp = client.get(f"/api/team/audit?target={target}", headers=_viewer(auth))

    assert resp.status_code == 200, resp.text
    events = resp.json()["events"]
    # Newest first, ordered by ts (see Observe.audit docstring for why not seq).
    assert [e["actor_did"] for e in events] == ["did:arc:a2", "did:arc:a1", "did:arc:a0"]
