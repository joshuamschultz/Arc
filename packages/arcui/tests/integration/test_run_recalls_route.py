"""SPEC-073 Phase D2 — GET /api/runs/{run_id}/recalls (run-scoped recall cards).

Seeds a ``memory.recall_attributed`` row onto the durable signed WORM chain the
same way ``test_team_aggregations.py::TestFleetAudit`` seeds ``audit_chain`` for
``/api/team/audit`` — appending directly to ``<data_dir>/worm/audit-chain.jsonl``,
the file ``StoreIngest`` mirrors into the ``audit_chain`` table that
``Observe.audit`` (and the D2 ``Observe.run_recalls``) reads through.

RED reason: neither ``Observe.run_recalls`` nor the
``GET /api/runs/{run_id}/recalls`` route exist yet, so every request below 404s
regardless of what was seeded.
"""

from __future__ import annotations

import json
from pathlib import Path

from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app


def _write_worm_recall_event(
    data_dir: Path,
    *,
    seq: int,
    request_id: str,
    cards: list[str],
    trigger: str,
) -> None:
    """Append one signed-chain ``memory.recall_attributed`` record to the WORM file."""
    worm = data_dir / "worm"
    worm.mkdir(parents=True, exist_ok=True)
    line = {
        "seq": seq,
        "event_hash": f"hash-{seq}",
        "prev_hash": f"hash-{seq - 1}" if seq else "",
        "signature": "sig",
        "event": {
            "ts": f"2026-05-31T00:00:{seq:02d}+00:00",
            "actor_did": "did:arc:memory-agent",
            "action": "memory.recall_attributed",
            "target": "memory",
            "outcome": "allow",
            "request_id": request_id,
            "extra": {"cards": cards, "trigger": trigger},
        },
    }
    with (worm / "audit-chain.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _viewer(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.viewer_token}"}


def test_run_recalls_route_returns_the_cards_attributed_to_that_run(
    _isolated_arc_data_dir: Path,
) -> None:
    _write_worm_recall_event(
        _isolated_arc_data_dir,
        seq=0,
        request_id="run-x",
        cards=["insight/foo"],
        trigger="task_start",
    )
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = create_app(auth_config=auth)

    with TestClient(app) as client:
        resp = client.get("/api/runs/run-x/recalls", headers=_viewer(auth))

    assert resp.status_code == 200, (
        f"D2 route/backend absent: GET /api/runs/run-x/recalls -> {resp.status_code} {resp.text}"
    )
    events = resp.json()["events"]
    assert events, "expected the seeded memory.recall_attributed event for run-x"
    cards = events[0].get("extra", {}).get("cards", [])
    assert "insight/foo" in cards


def test_run_recalls_route_is_empty_for_a_run_with_no_recalls(
    _isolated_arc_data_dir: Path,
) -> None:
    _write_worm_recall_event(
        _isolated_arc_data_dir,
        seq=0,
        request_id="run-x",
        cards=["insight/foo"],
        trigger="task_start",
    )
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = create_app(auth_config=auth)

    with TestClient(app) as client:
        resp = client.get("/api/runs/run-y/recalls", headers=_viewer(auth))

    assert resp.status_code == 200, (
        f"D2 route absent: GET /api/runs/run-y/recalls -> {resp.status_code} {resp.text}"
    )
    assert resp.json() == {"events": []}
