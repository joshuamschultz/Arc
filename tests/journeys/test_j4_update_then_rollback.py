"""J4 G7 — import v2 of a skill pack over v1, then roll back to v1.

The operator journey, on a real deployment (real Arc home, operator key, agent
identity, file-journal revision anchor, ArcAgent, arcui app); only the LLM wire is
absent because no turn runs:

1. ``arc capability-import import`` + ``promote`` installs v1 of a pack that carries
   ``scripts/`` and ``references/``. The running agent loads it.
2. The same commands with v2 do NOT fail with "already exists": v2 becomes a new
   operator-signed anchored revision, the original is enrolled as revision 1, and
   the agent loads v2, scripts included.
3. arcui ``POST .../rollback`` to revision 1 restores v1's exact bytes, and every
   file of the active revision carries the OPERATOR's signature again.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from arctrust import ArtifactSignature

_SKILL = "reporter"


def _body(step: str) -> bytes:
    return (
        "---\nname: reporter\ndescription: Create reports\n---\n"
        "## Resources\nnone\n## Contract\nfollow the steps\n"
        "## Knowledge\nsource data\n## Steps\n"
        f"{step}\n"
        "## Anti Patterns\nnone\n## Examples\nexample\n## Validation\ncheck\n"
    ).encode()


def _pack(version: str) -> dict[str, bytes]:
    return {
        "SKILL.md": _body(f"run scripts/extract.py ({version})"),
        "scripts/extract.py": f"print('extract {version}')\n".encode(),
        "references/schema.md": f"schema {version}\n".encode(),
    }


def _zip(path: Path, files: dict[str, bytes]) -> Path:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for relative, content in files.items():
            archive.writestr(f"skills/{_SKILL}/{relative}", content)
    path.write_bytes(buffer.getvalue())
    return path


def _cli_import_and_promote(
    archive: Path, agent_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from arccli.commands.capability_import import capability_import_handler

    capsys.readouterr()
    capability_import_handler(["import", "--agent", agent_id, "--json", str(archive)])
    review = json.loads(capsys.readouterr().out)
    capability_import_handler(["promote", "--agent", agent_id, review["import_id"]])


def _loaded_body(agent: Any) -> str | None:
    entry = next((item for item in agent.skills if item.name == _SKILL), None)
    if entry is None:
        return None
    if entry.read_current is not None:
        current: str | None = entry.read_current()
        return current
    return str(Path(entry.location).read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_j4_update_then_rollback(
    deployment: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import arcagent
    import arcui
    from arccli.commands._serve import build_skill_revision_anchor_factory
    from arccli.commands.agent._common import load_cli_agent
    from arccli.commands.operator import resolve_operator_signer
    from arcstore.backends.memory import FakeBackend
    from arctrust.policy import OperatorApprovalAuthority
    from arcui.auth import AuthConfig

    monkeypatch.chdir(deployment.team_root.parent)
    agent_dir = deployment.agent_dir
    from arcgateway import team_roster

    agent_id = next(
        entry.agent_id
        for entry in team_roster.list_team(team_root=deployment.team_root, online_ids=set())
    )
    folder = agent_dir / "capabilities" / "skills" / _SKILL
    operator_did = OperatorApprovalAuthority(resolve_operator_signer()).did

    # 1. v1 installs and loads.
    _cli_import_and_promote(_zip(deployment.home / "v1.zip", _pack("v1")), agent_id, capsys)
    assert (folder / "scripts" / "extract.py").read_bytes() == _pack("v1")["scripts/extract.py"]
    agent, _config, config_path = load_cli_agent(agent_dir)
    await agent.startup()
    try:
        assert _loaded_body(agent) == _pack("v1")["SKILL.md"].decode()

        # 2. v2 over v1 becomes revision 2; the original is revision 1.
        _cli_import_and_promote(_zip(deployment.home / "v2.zip", _pack("v2")), agent_id, capsys)
        factory = build_skill_revision_anchor_factory()
        assert factory is not None
        resolver = arcagent.AnchoredSkillRevisionResolver(
            agent_did=agent.did, config_path=config_path, anchor_factory=factory
        )
        history = resolver.revision_history(folder)
        assert [version for _, version, _, _ in history] == [2, 1]
        for relative, content in _pack("v2").items():
            assert resolver.read_verified_file(folder, relative) == content
        await agent.reload_or_raise()
        assert _loaded_body(agent) == _pack("v2")["SKILL.md"].decode()

        # 3. Roll back to v1 through the arcui operator route.
        auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
        app = arcui.create_app(
            auth_config=auth,
            team_root=deployment.team_root,
            skill_revision_anchor_factory=factory,
            arcstore_backend=FakeBackend(),
        )
        app.state.embedded_agent_cache = {agent.did: agent}
        operator = {"Authorization": "Bearer operator"}
        path = f"/api/agents/{agent_id}/skills/{_SKILL}"
        # In-loop ASGI client: the started agent and the route share one event loop,
        # exactly as the embedded fleet does inside ``arc ui``.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://arc") as client:
            versions = await client.get(path + "/versions", headers=operator)
            assert versions.status_code == 200, versions.text
            original = versions.json()["items"][-1]["candidate_id"]
            rollback = await client.post(
                path + "/rollback",
                headers=operator,
                json={"candidate_id": original, "confirm": True},
            )
            assert rollback.status_code == 200, rollback.text

        assert [version for _, version, _, _ in resolver.revision_history(folder)] == [3, 2, 1]
        assert _loaded_body(agent) == _pack("v1")["SKILL.md"].decode()
        active = resolver.active_folder(folder)
        assert active is not None
        for relative, content in _pack("v1").items():
            assert resolver.read_verified_file(folder, relative) == content
            sidecar = ArtifactSignature.from_json(
                (active / f"{relative}.arcsig").read_text(encoding="utf-8")
            )
            assert sidecar is not None
            assert sidecar.signer_did == operator_did
            assert sidecar.signer_did != agent.did
    finally:
        await agent.shutdown()
