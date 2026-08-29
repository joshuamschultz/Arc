"""Regression: H-013/H-014 — ONE consolidated tool list with the loader verdict.

The Tools tab used to render two near-identical tables: a policy/edit-affordance
table (name/transport/classification/status) and a second "CAPABILITY TOOLS —
LOADER VERDICTS" table (name/version/source/status/description) built from the
capability inventory seam. This regression locks the merge: `GET
/api/agents/{id}/tools` must return ONE row per tool carrying both — including
the loader/TOFU signature-provenance verdict (`loader_status`/`loader_detail`)
that proves a tool was signed and verified at load, and a normalized `source`
badge (H-014: builtin / agent / extension / module) derived from the loader's
scan root or the disk-scan transport.

The module-tool case installs a REAL signed module the way an operator does
(``arcbundle.build_bundle`` → ``verify_bundle`` → ``materialize`` →
``arcagent.trust_bundled_capabilities`` → ``copy_capabilities``), mirroring
``packages/arcagent/tests/security/test_module_capability_trust.py`` — an
unsigned per-agent module copy is denied as a whole file (SPEC-066 T-972), so
a "loaded" module-tool verdict cannot be faked without going through Sign/TOFU
for real.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import arcagent
import arcbundle
import pytest
from arcgateway import team_roster
from arctrust import generate_keypair
from arctrust.identity import AgentIdentity
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_routes
from arcui.routes.agent_detail import tools as tools_route

_ISSUER = "did:arc:hotfix-test-issuer"
_MODULE = "probe_hotfix"

#: A module folder is one only if it ships both files; discovery reads both.
_MODULE_CAPABILITIES = """\
from arcagent.tools._decorator import tool


@tool(description="Probe tool for the H-013/H-014 merge regression.", version="1.0.0", classification="read_only")
async def probe_hotfix_tool() -> str:
    return "ok"
"""
_MODULE_RUNTIME = """\
def state() -> None:
    return None
"""


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))


def _install_signed_module(tmp_path: Path, agent_dir: Path, config_path: Path) -> None:
    """Install + trust + per-agent-copy ``_MODULE``, the way `arc module
    install` does — see ``test_module_capability_trust.py``'s ``_install`` /
    ``_trust`` for the same recipe against arcagent's own loader directly."""
    source = tmp_path / "module-src" / _MODULE
    source.mkdir(parents=True)
    (source / "capabilities.py").write_text(_MODULE_CAPABILITIES, encoding="utf-8")
    (source / "_runtime.py").write_text(_MODULE_RUNTIME, encoding="utf-8")

    keypair = generate_keypair()
    bundle = arcbundle.build_bundle(
        source,
        module=_MODULE,
        version="1.0.0",
        private_key=keypair.private_key,
        issuer=_ISSUER,
        out=tmp_path / "bundles" / f"{_MODULE}.arcbundle",
    )
    verified = arcbundle.verify_bundle(
        bundle, tier="personal", trusted_issuers={_ISSUER: keypair.public_key}
    )
    module_dir = arcbundle.materialize(verified, arcagent.module_root())
    arcagent.trust_bundled_capabilities(
        module_dir, config_path=config_path, issuer_key=keypair.public_key, issuer_did=_ISSUER
    )
    arcbundle.copy_capabilities(module_dir, agent_dir, module=_MODULE)


def _agent(tmp_path: Path) -> tuple[TestClient, str]:
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    key_dir = tmp_path / "keys"
    identity.save_keys(key_dir)
    team_root = tmp_path / "team"
    agent_dir = team_root / "olivia_agent"
    workspace = agent_dir / "workspace"
    workspace.mkdir(parents=True)
    config_path = agent_dir / "arcagent.toml"

    config_path.write_text(
        '[agent]\nname = "olivia"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{workspace}"\n'
        '[llm]\nmodel = "test/model"\n'
        '[security]\ntier = "personal"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n'
        f"[modules.{_MODULE}]\nenabled = true\n",
        encoding="utf-8",
    )
    _install_signed_module(tmp_path, agent_dir, config_path)

    auth = AuthConfig({"viewer_token": "viewer"})
    app = Starlette(routes=agent_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = AgentRegistry()
    app.state.embedded_agent_cache = None
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    return TestClient(app), "olivia"


def _row(tools: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [t for t in tools if t["name"] == name]
    assert matches, f"{name!r} not in payload: {[t['name'] for t in tools]}"
    return matches[0]


def test_merged_payload_carries_loader_verdict_for_builtin_and_module_tool(
    tmp_path: Path,
) -> None:
    client, agent_id = _agent(tmp_path)
    resp = client.get(
        f"/api/agents/{agent_id}/tools",
        headers={"Authorization": "Bearer viewer"},
    )
    assert resp.status_code == 200
    body = resp.json()

    # H-010: the policy verdict this route already computed must survive the
    # merge untouched.
    assert "policy_summary" in body

    builtin = _row(body["tools"], "read")
    assert builtin["source"] == "builtin"
    assert builtin["loader_status"] == "loaded"  # the builtins root is TRUSTED
    assert builtin["version"]
    assert builtin["description"]

    module_tool = _row(body["tools"], "probe_hotfix_tool")
    assert module_tool["source"] == "module"
    assert module_tool["loader_status"] == "loaded"  # real Sign/TOFU verification passed
    assert module_tool["version"]
    assert module_tool["description"]


# ---------------------------------------------------------------------------
# Unit-level: the merge/categorization logic itself, independent of the real
# capability loader / TOFU pipeline above.
# ---------------------------------------------------------------------------


def test_source_category_covers_all_four_h014_badges() -> None:
    assert tools_route._source_category("builtin", "") == "builtin"
    assert tools_route._source_category("", "builtins-skills") == "builtin"
    assert tools_route._source_category("module:memory", "") == "module"
    assert tools_route._source_category("", "module:memory-skills") == "module"
    assert tools_route._source_category("extension", "") == "extension"
    # H-031: the loader's REAL scan-root spelling for a connector's tools is
    # "extension:<name>" (arcagent.capabilities.capability_loader.
    # EXTENSION_ROOT_PREFIX) — bare "extension" is only the legacy disk-scan
    # transport label. Without this branch a connector's live-registered
    # tools fell through to "agent" and the source filter could never find
    # them (H-031 coverage regression).
    assert tools_route._source_category("", "extension:github") == "extension"
    assert tools_route._source_category("agent_dir", "") == "agent"
    assert tools_route._source_category("workspace", "") == "agent"
    assert tools_route._source_category("registered", "") == "agent"


@pytest.mark.asyncio
async def test_extension_tool_with_no_static_scan_root_surfaces_as_extension(
    tmp_path: Path,
) -> None:
    """H-031 coverage: a connector's per-verb tool has no ``.py`` file any
    static scan root will ever find — it exists only in a live agent's
    runtime registry, tagged with its true ``extension:<name>`` scan root
    (H-030's ``RuntimeToolItem.source``). The merge must surface it anyway,
    correctly badged, rather than it being invisible everywhere the fleet
    Tools & Skills page reads.
    """
    from arcagent.core.tool_registry import RegisteredTool, ToolTransport

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    (agent_root / "arcagent.toml").write_text(
        '[agent]\nname = "ext-agent"\n\n[security]\ntier = "personal"\n',
        encoding="utf-8",
    )

    class _FakeLiveAgent:
        _identity = None

        @property
        def registered_tools(self) -> list[RegisteredTool]:
            return [
                RegisteredTool(
                    name="github_create_issue",
                    description="open a GitHub issue",
                    input_schema={},
                    transport=ToolTransport.PROCESS,
                    execute=None,
                    classification="external_effect",
                    source="extension:github",
                )
            ]

    tools: list[dict[str, Any]] = []
    await tools_route._merge_loader_verdicts(tools, agent_root, _FakeLiveAgent())

    row = _row(tools, "github_create_issue")
    assert row["source"] == "extension"
    assert row["loader_status"] == "loaded"


@pytest.mark.asyncio
async def test_merge_loader_verdicts_unions_rather_than_intersects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A loader-enumerated tool the disk/builtin scan missed must be ADDED to
    the merged list, not dropped — the merged table replaces the union of the
    two prior tables, never their intersection."""

    async def _fake_loader_rows(agent_root: Path, live_agent: Any) -> list[dict[str, Any]]:
        return [
            {
                "name": "read",
                "version": "1.0.0",
                "source_root": "builtins",
                "description": "Read a file.",
                "status": "loaded",
                "status_detail": "",
            },
            {
                "name": "only_in_loader",
                "version": "2.0.0",
                "source_root": "agent",
                "description": "Loader-only tool.",
                "status": "new_sighting",
                "status_detail": "awaiting approval",
            },
        ]

    monkeypatch.setattr(tools_route, "_loader_tool_rows", _fake_loader_rows)

    tools: list[dict[str, Any]] = [
        {
            "name": "read",
            "transport": "builtin",
            "classification": "read_only",
            "description": "",
            "status": "allow",
        },
    ]
    await tools_route._merge_loader_verdicts(tools, tmp_path, None)

    read_row = _row(tools, "read")
    assert read_row["version"] == "1.0.0"
    assert read_row["source"] == "builtin"
    assert read_row["loader_status"] == "loaded"
    assert read_row["description"] == "Read a file."  # filled in from the loader

    loader_only_row = _row(tools, "only_in_loader")
    assert loader_only_row["source"] == "agent"
    assert loader_only_row["loader_status"] == "new_sighting"
    assert loader_only_row["loader_detail"] == "awaiting approval"
