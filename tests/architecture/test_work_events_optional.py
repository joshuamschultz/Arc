"""SPEC-088 T-1373 (RED): arcui works with the notification components physically absent.

REQ-633: WHERE the callback dispatcher or the stream route is physically absent the system
SHALL still accept intake, record work events and run workflows, and SHALL report
notifications as unavailable rather than fail at import or startup.

What is proven, on the real ``create_app`` with its real lifespan (``TestClient`` as a
context manager runs startup and shutdown):

1. With ``arcui.work_events`` (the COMP-013 package: dispatcher and policy) hidden from
   import, and separately with ``arcui.routes.work_events`` (COMP-014) hidden, and with
   both hidden, the app starts and stops cleanly.
2. ``POST /api/runs`` without ``callback_url`` still answers 202 and its ``links`` report
   ``notifications: "unavailable"`` (wherever inside ``links`` the builder puts it).
3. Work events are still appended: driving the intake row to a terminal state through
   the app's own ``app.state.task_store`` appends one ``run.finished`` event for that
   ``run_id`` to the work-event log over the app's own ``app.state.arcstore_backend``
   (``TaskStore`` is built with ``work_events=WorkEventLog(backend)`` in ``create_app``).
4. With ``arcui.routes.work_events`` hidden, the work-event routes are simply not served
   (404), not a 500.

A control case with nothing hidden runs the same journey, so a failure in the hidden
cases is about absence, not the harness.

"Hidden" is the canonical-path ``sys.modules[name] = None`` patch (any import of the name
or a submodule raises ``ImportError``), applied after evicting every already-imported
``arcui`` module so ``arcui.server`` is imported fresh under the absence. Everything is
restored at teardown. Hermetic: in-memory arcstore backend, no NATS server, Arc home and
team under ``tmp_path``.
"""

from __future__ import annotations

import importlib
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.identity import did_from_public_key
from arctrust.keypair import generate_keypair

OPERATOR_TOKEN = "o" * 64
VIEWER_TOKEN = "v" * 64
DISPATCHER_PKG = "arcui.work_events"
STREAM_ROUTES = "arcui.routes.work_events"
SALES = did_from_public_key(generate_keypair().public_key, org="default", agent_type="executor")
ACCEPTING = "[modules.tasks]\nenabled = true\n\n[modules.tasks.config]\ndispatch = true\n"

CASES = {
    "nothing_hidden": (),
    "dispatcher_package_hidden": (DISPATCHER_PKG,),
    "stream_routes_hidden": (STREAM_ROUTES,),
    "both_hidden": (DISPATCHER_PKG, STREAM_ROUTES),
}


@pytest.fixture
def hidden(request: pytest.FixtureRequest) -> Iterator[tuple[str, ...]]:
    """Evict arcui, mark the named modules absent, restore everything afterwards."""
    names: tuple[str, ...] = request.param
    evicted = {name: mod for name, mod in sys.modules.items() if name.split(".")[0] == "arcui"}
    for name in evicted:
        del sys.modules[name]
    for name in names:
        sys.modules[name] = None  # type: ignore[assignment]  # reason: canonical "absent" marker
    try:
        yield names
    finally:
        for name in [n for n in sys.modules if n.split(".")[0] == "arcui"]:
            del sys.modules[name]
        sys.modules.update(evicted)


@pytest.fixture
def team_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "arcstore"))
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path))
    monkeypatch.setenv("ARCTEAM_NATS_URL", "")
    root = tmp_path / "team"
    folder = root / "sales_agent"
    (folder / "workspace").mkdir(parents=True)
    (folder / "arcagent.toml").write_text(
        '[agent]\nname = "sales"\norg = "default"\ntype = "executor"\n\n'
        f'[identity]\ndid = "{SALES}"\n\n{ACCEPTING}',
        encoding="utf-8",
    )
    return root


def _no_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer as an already-running broker would (mirrors arcui's test conftest)."""
    import arcteam.nats_server as nats_server

    async def _already_running(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(nats_server, "ensure_nats_server", _already_running)
    consumer = importlib.import_module("arcgateway.broker_bootstrap")
    monkeypatch.setattr(consumer, "ensure_nats_server", _already_running)


def _build_app(team_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    backend = FakeBackend()
    server = importlib.import_module("arcui.server")
    observe = importlib.import_module("arcui.observe")
    monkeypatch.setattr(server, "open_backend", lambda **_kw: backend)
    monkeypatch.setattr(observe, "open_backend", lambda **_kw: backend)
    _no_broker(monkeypatch)
    from arcui.auth import AuthConfig

    return server.create_app(
        auth_config=AuthConfig({"operator_token": OPERATOR_TOKEN, "viewer_token": VIEWER_TOKEN}),
        team_root=team_root,
        data_dir=tmp_path / "arcui-data",
        arcstore_backend=backend,
    )


def _notifications(links: Any) -> list[Any]:
    """Every value stored under a ``notifications`` key anywhere inside ``links``."""
    found: list[Any] = []
    if isinstance(links, dict):
        for key, value in links.items():
            if key == "notifications":
                found.append(value)
            found.extend(_notifications(value))
    elif isinstance(links, list):
        for item in links:
            found.extend(_notifications(item))
    return found


@pytest.mark.parametrize("hidden", list(CASES.values()), ids=list(CASES), indirect=True)
def test_intake_and_work_events_survive_absent_notification_components(
    hidden: tuple[str, ...], team_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.testclient import TestClient

    for name in hidden:
        with pytest.raises(ImportError):
            importlib.import_module(name)

    app = _build_app(team_root, tmp_path, monkeypatch)
    headers = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}

    with TestClient(app, raise_server_exceptions=False) as client:  # runs the lifespan
        resp = client.post(
            "/api/runs",
            json={"agent": SALES, "task": "Summarise the open orders"},
            headers={**headers, "Idempotency-Key": f"key-{uuid.uuid4().hex}"},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "queued"
        assert _notifications(body["links"]) == ["unavailable"], body["links"]
        run_id = body["run_id"]

        events = client.portal.call(_finish_and_read, app, run_id)  # type: ignore[union-attr]  # reason: portal exists inside the context

        if STREAM_ROUTES in hidden:
            page = client.get("/api/work-events", params={"agent": SALES}, headers=headers)
            assert page.status_code == 404, page.text

    assert [(e.kind, e.run_id) for e in events] == [("run.finished", run_id)]


async def _finish_and_read(app: Any, run_id: str) -> list[Any]:
    """Settle the intake row as the agent would, then read the log for that run."""
    from arcstore.work_events import WorkEventLog

    finished = await app.state.task_store.finish(
        f"intake-{run_id}", status="done", resolution="completed", actor_did=SALES
    )
    assert finished is not None, "the intake row was not created"
    log = WorkEventLog(app.state.arcstore_backend)
    return await log.read(0, limit=100, where={"run_id": run_id})
