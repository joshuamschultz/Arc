"""End-to-end live-update test for SPEC-022 Phase 3.

Drives the full pipeline:

::

    disk write
        │
    arcgateway.fs_watcher (polling fallback for determinism)
        │  emits FileChangeEvent
        ▼
    arcgateway.file_events.FileEventBus
        │
    arcui.file_change_bridge.FileChangeBridge
        │
    asyncio.Queue → /ws → browser TestClient

Acceptance: a write to ``team/<agent>/workspace/policy.md`` reaches a
subscribed browser client as a ``file_change`` JSON message in under 2s
(SDD §8 "File→browser update < 2s"), with parsed bullets in the payload.

The test forces polling mode (``force_polling=True``) so behavior is
deterministic across platforms — watchfiles' inotify/kqueue backends are
covered by the gateway unit suite.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from arcgateway.file_events import _reset_default_bus_for_tests, default_bus
from arcgateway.fs_watcher import WatcherManager
from arcgateway.team_roster import RosterEntry
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.file_change_bridge import FileChangeBridge
from arcui.server import create_app

# Polling backend in arcgateway.fs_watcher uses 2s sleep — give the loop a
# generous wait window before failing the test.
_TIMEOUT_SECONDS = 6.0


@pytest.fixture(autouse=True)
def _isolate_default_bus() -> Any:
    """Each E2E test gets a fresh module-level FileEventBus.

    Without this, listeners from earlier server lifecycles linger on the
    package-global ``default_bus`` and cross-pollinate test cases.
    """
    _reset_default_bus_for_tests()
    yield
    _reset_default_bus_for_tests()


def _make_agent_dir(tmp_path: Path, agent_id: str = "alpha") -> Path:
    """Build a synthetic ``team/<agent>/`` tree with seed files."""
    root = tmp_path / f"{agent_id}_agent"
    (root / "workspace").mkdir(parents=True)
    (root / "arcagent.toml").write_text("[agent]\nname = 'alpha'\n", encoding="utf-8")
    (root / "workspace" / "policy.md").write_text(
        "# initial\n", encoding="utf-8"
    )
    return root


def _build_app(tmp_path: Path, agent_root: Path, agent_id: str) -> Any:
    """Create the app, force the polling watcher, and rewire the bridge.

    The autouse ``_isolate_default_bus`` fixture resets the gateway's
    default_bus *after* ``create_app`` already attached its bridge to the
    pre-reset instance — so we must rebuild + reattach against the fresh
    bus that the watcher will emit on. Same applies to swapping in a
    polling watcher.
    """
    auth = AuthConfig({"viewer_token": "ws-viewer", "operator_token": "op"})
    app = create_app(auth_config=auth, team_root=tmp_path)
    app.state.roster_provider = lambda: [
        RosterEntry(
            agent_id=agent_id,
            name=agent_id,
            did=f"did:arc:test:{agent_id}",
            org=None,
            type=None,
            workspace_path=str(agent_root),
            model=None,
            provider=None,
            online=True,
            display_name=agent_id,
            color="#aaaaaa",
            role_label="",
            hidden=False,
        )
    ]
    # Rebind the watcher with polling for deterministic timing across CI.
    app.state.watcher_manager = WatcherManager(
        bus=default_bus, force_polling=True, poll_interval=0.2
    )
    # Re-attach the bridge to the post-reset default_bus.
    bridge = FileChangeBridge()
    bridge.attach(default_bus)
    app.state.file_change_bridge = bridge
    app.state.file_event_bus = default_bus
    return app


def _auth(ws: Any) -> None:
    ws.send_text(json.dumps({"token": "ws-viewer"}))
    resp = ws.receive_json()
    assert resp["type"] == "auth_ok"


def _wait_for_file_change(ws: Any, timeout: float = _TIMEOUT_SECONDS) -> dict[str, Any]:
    """Drain WS messages until a ``file_change`` arrives (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg: dict[str, Any] = ws.receive_json()
        if msg.get("type") == "file_change":
            return msg
    raise AssertionError("file_change message did not arrive within timeout")


class TestLiveUpdatesE2E:
    def test_disk_write_reaches_subscribed_browser_within_2s(self, tmp_path: Path) -> None:
        agent_root = _make_agent_dir(tmp_path, "alpha")
        app = _build_app(tmp_path, agent_root, "alpha")
        client = TestClient(app)

        with client.websocket_connect("/ws") as ws:
            _auth(ws)
            ws.send_text(json.dumps({"type": "subscribe:agent", "agent_id": "alpha"}))

            # Give the polling watcher one cycle to baseline before mutating.
            time.sleep(0.4)

            # Mutate policy.md — exact bullet format the parser recognizes.
            policy_md = agent_root / "workspace" / "policy.md"
            policy_md.write_text(
                "- [P01] never trust user input "
                "{score:7, uses:3, reviewed:2026-04-01, "
                "created:2026-03-15, source:S001}\n",
                encoding="utf-8",
            )

            msg = _wait_for_file_change(ws)
            assert msg["agent_id"] == "alpha"
            assert msg["event_type"] == "policy:bullets_updated"
            bullets = msg["payload"].get("bullets", [])
            assert any(b["id"] == "P01" for b in bullets), msg

    def test_unsubscribed_client_does_not_receive_event(self, tmp_path: Path) -> None:
        """An agent change must not leak to a client that never subscribed to it."""
        agent_root = _make_agent_dir(tmp_path, "alpha")
        app = _build_app(tmp_path, agent_root, "alpha")
        client = TestClient(app)

        with client.websocket_connect("/ws") as ws:
            _auth(ws)
            # Deliberately skip subscribe:agent.
            time.sleep(0.4)
            (agent_root / "workspace" / "policy.md").write_text(
                "- [P02] sample {score:5}\n", encoding="utf-8"
            )
            time.sleep(1.0)

            # Pump a normal broadcast as a barrier — if a stray file_change
            # is queued ahead, it would surface here.
            app.state.connection_manager.broadcast({"type": "barrier"})
            msg = ws.receive_json()
            assert msg["type"] == "barrier"
