"""Signed skill edit reaches the live provider through the agent route."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.capabilities.capability_signing import sign
from arcagent.capabilities.provider import AgentCapabilityProvider, _Skill
from arcagent.modules.capability_import.revisions import AnchoredSkillRevisionResolver
from arctrust import AnchorHead, InProcessSigner
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.agent_detail.skill_versions import (
    get_skill_evals,
    get_skill_version_body,
    get_skill_version_diff,
    get_skill_versions,
    post_skill_promote_golden,
    post_skill_rollback,
)
from arcui.routes.agent_detail.skills import get_skill_detail, put_skill_revision

_DID = "did:arc:agent:ada"
_OPERATOR = "did:arc:operator:test"


def _body(step: str) -> bytes:
    return (
        "---\nname: reporter\ndescription: Create reports\n---\n"
        "## Resources\nnone\n## Contract\nfollow the steps\n"
        "## Knowledge\nsource data\n## Steps\n"
        f"{step}\n"
        "## Anti Patterns\nnone\n## Examples\nexample\n## Validation\ncheck\n"
    ).encode()


class _Anchor:
    def __init__(self, did: str = _DID) -> None:
        self.scope = f"skill/{hashlib.sha256(did.encode()).hexdigest()}/reporter"
        self.head: AnchorHead | None = None

    def latest(self) -> AnchorHead | None:
        return self.head

    def compare_and_advance(
        self, expected: AnchorHead | None, digest: str, intent: str
    ) -> AnchorHead:
        assert expected == self.head
        self.head = AnchorHead(
            scope=self.scope,
            version=expected.version + 1 if expected else 1,
            digest=digest,
            previous_digest=expected.digest if expected else None,
            intent=intent,
        )
        return self.head


class _LiveAgent:
    def __init__(self, loader: CapabilityLoader, registry: CapabilityRegistry) -> None:
        self.loader = loader
        self.registry = registry
        self.registered_tools: list[object] = []
        self.loaded_content: str | None = None

    @property
    def skills(self) -> list[object]:
        return list(self.registry.skill_entries())

    async def reload_or_raise(self) -> None:
        await self.loader.reload()
        entry = await self.registry.get_skill("reporter")
        assert entry is not None
        provider = AgentCapabilityProvider(
            tools=[],
            skills=[
                _Skill(
                    name=entry.name,
                    description=entry.description,
                    location=entry.location,
                    scan_root=entry.scan_root,
                    read_current=entry.read_current,
                )
            ],
            tier="federal",
            caller_did=_DID,
        )
        self.loaded_content = await provider.load("reporter", caller_did=_DID)


def test_signed_revision_route_reloads_live_provider(tmp_path: Path) -> None:
    root = tmp_path / "ada"
    folder = root / "capabilities" / "skills" / "reporter"
    folder.mkdir(parents=True)
    (root / "workspace").mkdir()
    config = root / "arcagent.toml"
    config.write_text('[agent]\nname = "ada"\n\n[security]\ntier = "federal"\n')
    skill = folder / "SKILL.md"
    skill.write_bytes(_body("old"))
    signer = InProcessSigner(bytes(range(32)))
    sign(skill, signer_did=_OPERATOR, signer=signer, config_path=config)
    anchor = _Anchor()
    resolver = AnchoredSkillRevisionResolver(
        agent_did=_DID, config_path=config, anchor_factory=lambda _did, _name: anchor
    )
    registry = CapabilityRegistry()
    loader = CapabilityLoader(
        scan_roots=[("agent-skills", folder.parent)],
        registry=registry,
        skill_artifact_resolver=resolver,
        require_signature=True,
        trusted_public_keys=(signer.public_key,),
    )
    live = _LiveAgent(loader, registry)
    app = Starlette(
        routes=[
            Route("/api/agents/{id}/skills/{skill_name}/detail", get_skill_detail),
            Route(
                "/api/agents/{id}/skills/{skill_name}/revision",
                put_skill_revision,
                methods=["PUT"],
            ),
            Route("/api/agents/{id}/skills/{skill_name}/versions", get_skill_versions),
            Route("/api/agents/{id}/skills/{skill_name}/evals", get_skill_evals),
            Route(
                "/api/agents/{id}/skills/{skill_name}/promote",
                post_skill_promote_golden,
                methods=["POST"],
            ),
            Route("/api/agents/{id}/skills/{skill_name}/versions/diff", get_skill_version_diff),
            Route(
                "/api/agents/{id}/skills/{skill_name}/versions/{candidate_id}/body",
                get_skill_version_body,
            ),
            Route(
                "/api/agents/{id}/skills/{skill_name}/rollback",
                post_skill_rollback,
                methods=["POST"],
            ),
        ]
    )
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.roster_provider = lambda: [
        SimpleNamespace(agent_id="ada", did=_DID, workspace_path=root)
    ]
    app.state.embedded_agent_cache = {_DID: live}
    app.state.operator_signer_factory = lambda: signer
    app.state.skill_revision_anchor_factory = lambda _did, _name: anchor
    client_context = TestClient(app)
    client = client_context.__enter__()
    path = "/api/agents/ada/skills/reporter"
    original = client.get(path + "/detail", headers={"Authorization": "Bearer operator"})
    assert original.status_code == 200, original.text
    revised = client.put(
        path + "/revision",
        headers={"Authorization": "Bearer operator"},
        json={"content": _body("new").decode(), "expected_sha256": original.json()["sha256"]},
    )
    assert revised.status_code == 200, revised.text
    assert anchor.head is not None
    assert live.loaded_content == _body("new").decode()
    detail = client.get(path + "/detail", headers={"Authorization": "Bearer viewer"})
    assert detail.status_code == 200
    assert detail.json()["content"] == _body("new").decode()
    first_digest = anchor.head.digest
    second = client.put(
        path + "/revision",
        headers={"Authorization": "Bearer operator"},
        json={"content": _body("newer").decode(), "expected_sha256": detail.json()["sha256"]},
    )
    assert second.status_code == 200, second.text
    versions = client.get(path + "/versions", headers={"Authorization": "Bearer viewer"})
    assert versions.status_code == 200
    assert [row["generation"] for row in versions.json()["items"]] == [2, 1]
    old_body = client.get(
        path + f"/versions/{first_digest}/body", headers={"Authorization": "Bearer viewer"}
    )
    assert old_body.json()["body"] == _body("new").decode()
    diff = client.get(
        path + f"/versions/diff?a={first_digest}&b={anchor.head.digest}",
        headers={"Authorization": "Bearer viewer"},
    )
    assert diff.status_code == 200
    assert "+newer" in diff.json()["diff"]
    before_rollback = anchor.head
    denied_viewer = client.post(
        path + "/rollback",
        json={"candidate_id": first_digest, "confirm": True},
        headers={"Authorization": "Bearer viewer"},
    )
    assert denied_viewer.status_code == 403
    missing_confirm = client.post(
        path + "/rollback",
        json={"candidate_id": first_digest},
        headers={"Authorization": "Bearer operator"},
    )
    assert missing_confirm.status_code == 400
    unknown = client.post(
        path + "/rollback",
        json={"candidate_id": "0" * 64, "confirm": True},
        headers={"Authorization": "Bearer operator"},
    )
    assert unknown.status_code == 404
    app.state.embedded_agent_cache = {}
    no_runtime = client.post(
        path + "/rollback",
        json={"candidate_id": first_digest, "confirm": True},
        headers={"Authorization": "Bearer operator"},
    )
    assert no_runtime.status_code == 503
    assert anchor.head == before_rollback
    app.state.embedded_agent_cache = {_DID: live}
    rollback = client.post(
        path + "/rollback",
        json={"candidate_id": first_digest, "confirm": True},
        headers={"Authorization": "Bearer operator"},
    )
    assert rollback.status_code == 200, rollback.text
    assert anchor.head.version == 3
    assert live.loaded_content == _body("new").decode()
    curated = client.post(
        path + "/promote",
        json={"case_id": "reviewed", "gate_type": "exact_match", "ideal_output": "approved"},
        headers={"Authorization": "Bearer operator"},
    )
    assert curated.status_code == 200, curated.text
    assert anchor.head.version == 4
    evals = client.get(path + "/evals", headers={"Authorization": "Bearer viewer"})
    assert evals.status_code == 200
    assert curated.json()["nodeid"] in {item["nodeid"] for item in evals.json()["items"]}
    versions_after_curation = client.get(
        path + "/versions", headers={"Authorization": "Bearer viewer"}
    )
    assert [row["generation"] for row in versions_after_curation.json()["items"]] == [4, 3, 2, 1]
    current_head = anchor.head

    class _StaleRuntime:
        def __init__(self) -> None:
            self.skills = [replace(entry, read_current=lambda: "stale") for entry in live.skills]
            self.registered_tools: list[object] = []

        async def reload_or_raise(self) -> None:
            return None

    app.state.embedded_agent_cache = {_DID: _StaleRuntime()}
    unchanged_runtime = client.post(
        path + "/rollback",
        json={"candidate_id": before_rollback.digest, "confirm": True},
        headers={"Authorization": "Bearer operator"},
    )
    assert unchanged_runtime.status_code == 503
    assert anchor.head.version == current_head.version + 1
    app.state.embedded_agent_cache = {_DID: live}

    bob_did = "did:arc:agent:bob"
    bob_root = tmp_path / "bob"
    bob_folder = bob_root / "capabilities" / "skills" / "reporter"
    bob_folder.mkdir(parents=True)
    (bob_root / "workspace").mkdir()
    (bob_root / "arcagent.toml").write_text(
        '[agent]\nname = "bob"\n\n[security]\ntier = "federal"\n'
    )
    bob_skill = bob_folder / "SKILL.md"
    bob_skill.write_bytes(_body("bob"))
    sign(bob_skill, signer_did=_OPERATOR, signer=signer, config_path=bob_root / "arcagent.toml")
    bob_anchor = _Anchor(bob_did)
    app.state.skill_revision_anchor_factory = lambda did, _name: (
        bob_anchor if did == bob_did else anchor
    )
    app.state.roster_provider = lambda: [
        SimpleNamespace(agent_id="ada", did=_DID, workspace_path=root),
        SimpleNamespace(agent_id="bob", did=bob_did, workspace_path=bob_root),
    ]
    bob_versions = client.get(
        "/api/agents/bob/skills/reporter/versions", headers={"Authorization": "Bearer viewer"}
    )
    assert bob_versions.status_code == 503
    bob_body = client.get(
        f"/api/agents/bob/skills/reporter/versions/{first_digest}/body",
        headers={"Authorization": "Bearer viewer"},
    )
    assert bob_body.status_code == 503
    bob_diff = client.get(
        f"/api/agents/bob/skills/reporter/versions/diff?a={first_digest}&b={anchor.head.digest}",
        headers={"Authorization": "Bearer viewer"},
    )
    assert bob_diff.status_code == 503
    client_context.__exit__(None, None, None)
