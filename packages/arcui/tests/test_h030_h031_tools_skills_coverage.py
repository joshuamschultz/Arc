"""H-030 (signed upload -> loaded -> listed) and H-031 (source + loader-verdict
coverage on the fleet Tools & Skills page) — end to end through the REAL
upload route, the REAL capability loader, and the REAL fleet aggregation.

This is the test the ALPHA-HOTFIXES H-030 entry asked for: "Verified end-to-
end." Before this file, nothing proved that a capability an operator promoted
through the dashboard actually came back out the other end as a loaded,
listed capability with the same provenance the per-agent Tools tab shows —
only that the promotion call itself wrote signed bytes to disk.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from arcagent.capabilities.inventory import collect_agent_capability_inventory
from arctrust import OperatorKey, default_operator_key_path
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.registry import AgentRegistry
from arcui.routes.capability_imports import routes as import_routes
from arcui.routes.team_pages import routes as team_routes

_TOOL = (
    b"from arcagent import tool\n"
    b"@tool(description='does a thing', version='1.0.0')\n"
    b"async def h030_tool() -> str:\n"
    b"    return 'ok'\n"
)
_SKILL = b"""---
name: h030_skill
version: 1.0.0
description: an uploaded skill
triggers: [h030]
tools: [reload]
---

## Resources

## Contract

## Knowledge

## Steps

Use it.

## Anti Patterns

## Examples

## Validation
"""


def _archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("tools/h030_tool.py", _TOOL)
        archive.writestr("skills/h030_skill/SKILL.md", _SKILL)
    return buffer.getvalue()


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    agent_root = tmp_path / "team" / "ada_agent"
    agent_root.mkdir(parents=True)
    (agent_root / "arcagent.toml").write_text(
        '[agent]\nname = "ada"\n\n[security]\ntier = "federal"\n', encoding="utf-8"
    )
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=[*import_routes, *team_routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = SimpleNamespace(audit_event=lambda *args, **kwargs: None)
    app.state.agent_registry = AgentRegistry()
    app.state.roster_provider = lambda: [
        SimpleNamespace(
            agent_id="ada",
            display_name="Ada",
            did="did:arc:agent:ada",
            workspace_path=str(agent_root),
        )
    ]
    return TestClient(app), agent_root


def test_uploaded_capability_is_signed_loaded_and_listed_on_the_fleet_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    client, agent_root = _client(tmp_path)

    uploaded = client.post(
        "/api/agents/ada/capability-imports",
        headers={"Authorization": "Bearer viewer"},
        files={"file": ("bundle.zip", _archive(), "application/zip")},
    )
    assert uploaded.status_code == 201
    import_id = uploaded.json()["import_id"]

    promoted = client.post(
        f"/api/agents/ada/capability-imports/{import_id}/promote",
        headers={"Authorization": "Bearer operator"},
    )
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "promoted"

    # SIGNED — every promoted file carries its own detached signature.
    tool_artifact = agent_root / "capabilities" / "h030_tool.py"
    skill_artifact = agent_root / "capabilities" / "skills" / "h030_skill" / "SKILL.md"
    assert tool_artifact.is_file()
    assert tool_artifact.with_name("h030_tool.py.arcsig").is_file()
    assert skill_artifact.is_file()
    assert skill_artifact.with_name("SKILL.md.arcsig").is_file()

    # LOADED — the same seam a real agent load, `arc trust`, and arcui's
    # capability views all use reports it "loaded", not merely written.
    inventory = asyncio.run(collect_agent_capability_inventory(agent_root / "arcagent.toml"))
    by_name = {item.name: item for item in inventory.items}
    assert by_name["h030_tool"].status == "loaded", by_name["h030_tool"].status_detail
    assert by_name["h030_skill"].status == "loaded", by_name["h030_skill"].status_detail

    # LISTED — the fleet Tools & Skills endpoint shows it, carrying the
    # H-013/H-014 source + loader-verdict fields needed for H-031 filtering.
    listing = client.get("/api/team/tools-skills", headers={"Authorization": "Bearer viewer"})
    assert listing.status_code == 200
    body = listing.json()

    tool_row = next(t for t in body["tools"] if t["name"] == "h030_tool")
    assert tool_row["source"] == "agent"
    assert tool_row["loader_status"] == "loaded"
    assert tool_row["agents"] == ["ada"]

    skill_row = next(s for s in body["skills"] if s["name"] == "h030_skill")
    assert skill_row["source"] == "agent"
    assert skill_row["status"] == "loaded"
    assert skill_row["agent_id"] == "ada"

    # COVERAGE (H-031) — builtins are a distinct, always-present source, so a
    # correct fleet listing never collapses everything into one bucket.
    builtin_row = next(t for t in body["tools"] if t["name"] == "read")
    assert builtin_row["source"] == "builtin"


def test_revoked_capability_disappears_from_the_fleet_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    client, _ = _client(tmp_path)

    uploaded = client.post(
        "/api/agents/ada/capability-imports",
        headers={"Authorization": "Bearer viewer"},
        files={"file": ("bundle.zip", _archive(), "application/zip")},
    )
    import_id = uploaded.json()["import_id"]
    client.post(
        f"/api/agents/ada/capability-imports/{import_id}/promote",
        headers={"Authorization": "Bearer operator"},
    )

    revoked = client.post(
        f"/api/agents/ada/capability-imports/{import_id}/revoke",
        headers={"Authorization": "Bearer operator"},
    )
    assert revoked.status_code == 200

    listing = client.get("/api/team/tools-skills", headers={"Authorization": "Bearer viewer"})
    names = {t["name"] for t in listing.json()["tools"]}
    skill_names = {s["name"] for s in listing.json()["skills"]}
    assert "h030_tool" not in names
    assert "h030_skill" not in skill_names
