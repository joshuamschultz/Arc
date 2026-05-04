"""End-to-end tests for the SPEC-023 web chat pipeline.

Skipped unless ``ARC_E2E=1`` so they don't run in standard CI but are
available for local dress-rehearsal. The full-chat-turn case wants a
real ``team/`` directory; it self-skips if that isn't present.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from arcgateway.config import GatewayConfig
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

pytestmark = pytest.mark.skipif(
    os.environ.get("ARC_E2E") != "1",
    reason="E2E tests require ARC_E2E=1",
)

VIEWER_TOKEN = "viewer-tok-e2e"


def test_full_chat_turn_with_real_agent(tmp_path: Path) -> None:
    """A real ArcAgent loaded from ``team/`` answers a browser prompt.

    The test spins up a TestClient with a gateway_config that points at
    the local ``team/`` directory. If no ``team/concierge_agent`` is
    present we skip — the operator runs this only when the demo agent
    is in place.
    """
    repo_root = Path(__file__).resolve().parents[5]
    team_root = repo_root / "team"
    if not (team_root / "concierge_agent").is_dir():
        pytest.skip("no concierge_agent in team/; set up local team to run this test")

    cfg = GatewayConfig.from_toml_str("[platforms.web]\nenabled = true\n")
    auth = AuthConfig({"viewer_token": VIEWER_TOKEN, "operator_token": "op", "agent_token": "ag"})
    app = create_app(team_root=team_root, auth_config=auth, gateway_config=cfg)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/chat/concierge") as ws:
            ws.send_json({"token": VIEWER_TOKEN})
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            ws.send_json({"type": "message", "text": "What is your name?"})
            # We can't predict the LLM's output verbatim — wait for any
            # message frame from the agent within reasonable bounds.
            for _ in range(30):
                frame = ws.receive_json()
                if frame.get("type") == "message" and frame.get("from") == "agent":
                    return
            raise AssertionError("did not receive agent message")


def test_air_gap_no_external_references_in_static_bundle() -> None:
    """No served static asset references an external CDN.

    Walks every file under ``arcui/static/`` and asserts no occurrence
    of common CDN hostnames. Air-gap-ready (NFR-9 / FR-26).
    """
    static_root = Path(__file__).resolve().parents[2] / "src" / "arcui" / "static"
    suspicious = re.compile(
        r"(fonts\.googleapis\.com|fonts\.gstatic\.com|cdnjs\.cloudflare\.com|"
        r"cdn\.jsdelivr\.net|unpkg\.com|fontawesome\.com)"
    )
    # Only HTML/CSS/JS files actually load assets in a browser; READMEs and
    # other docs are not request-path content even though StaticFiles will
    # serve them on direct GET.
    browser_suffixes = {".html", ".css", ".js"}
    offenders: list[tuple[Path, str]] = []
    for path in static_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in browser_suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        match = suspicious.search(text)
        if match:
            offenders.append((path.relative_to(static_root), match.group(0)))
    assert not offenders, (
        "external CDN references found in served static bundle:\n"
        + "\n".join(f"  {p}: {m}" for p, m in offenders)
    )
