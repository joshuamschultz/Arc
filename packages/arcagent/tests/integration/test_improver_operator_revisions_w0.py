"""W0-skill (h): an applied improver change is an operator-signed anchored revision.

Drives the real skills module (``configure`` → ``skills_ready`` → the selected
``ArcSkillImprover``) over a real promoted import, a real file-journal anchor and
the operator signer. Only the eval sandbox and the eval model are faked. The
improved body must show up in ``revision_history`` (so versions and rollback see
it), every sidecar in the new revision must be the operator's, and the agent's own
DID key must never sign. With no anchored authority the improver refuses.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from arcskill.improver.models import BundleView, Candidate, EvalCase, EvalOutcome, OptimizeResult
from arctrust import ArtifactSignature, FileJournalAnchor, InProcessSigner
from arctrust.identity import AgentIdentity
from arctrust.policy import OperatorApprovalAuthority

from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.models import CapabilityImportLimits
from arcagent.modules.capability_import.revisions import (
    AnchoredSkillRevisionResolver,
    skill_revision_scope,
)
from arcagent.modules.capability_import.service import CapabilityImportService
from arcagent.modules.skills import _runtime
from arcagent.modules.skills.capabilities import skills_ready


def _body(step: str) -> bytes:
    return (
        "---\nname: reporter\ndescription: Create reports\n---\n"
        "## Resources\nnone\n## Contract\nfollow the steps\n"
        "## Knowledge\nsource data\n## Steps\n"
        f"{step}\n"
        "## Anti Patterns\nnone\n## Examples\nexample\n## Validation\ncheck\n"
    ).encode()


class _Runner:
    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        better = "carefully" in view.text
        return [EvalOutcome(case_id=c.id, passed=c.id.endswith("test_a") or better) for c in cases]


class _LLM:
    async def invoke(self, prompt: str) -> str:
        return ""


class _Entry:
    def __init__(self, name: str, location: Path) -> None:
        self.name = name
        self.location = location


class _Registry:
    def __init__(self, entry: _Entry) -> None:
        self._entry = entry

    def skill_entries(self) -> list[_Entry]:
        return [self._entry]

    def skill_entry(self, name: str) -> _Entry | None:
        return self._entry if name == self._entry.name else None

    async def suppressed_skills(self) -> list[str]:
        return []

    async def suppress_skill(self, name: str) -> None:
        return None

    async def unsuppress_skill(self, name: str) -> None:
        return None


class _Ctx:
    def __init__(self, **data: Any) -> None:
        self.data = data


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


@pytest.fixture
def _fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("arcskill.improver.improver.HubEvalRunner", lambda tier: _Runner())
    monkeypatch.setattr(_runtime, "_eval_invoker", lambda *_a: _LLM())

    async def optimize(self: Any, skill_name: str, current: str, traces: Any, **_: Any) -> Any:
        best = Candidate(
            id="abc123def456",
            text=_body("do it carefully").decode(),
            generation=1,
            aggregate_scores={"a": 2.0},
        )
        return OptimizeResult(
            skill_name=skill_name,
            best_candidate=best,
            frontier=[best],
            iterations_run=1,
            stop_reason="done",
            seed_scores={"a": 1.0},
            improvement={"a": 1.0},
        )

    monkeypatch.setattr("arcskill.improver.engine.SkillOptimizer.optimize", optimize)


def _promoted(
    tmp_path: Path, agent_did: str, signer: InProcessSigner
) -> tuple[Path, AnchoredSkillRevisionResolver]:
    root = tmp_path / "agent" / "capabilities"
    root.mkdir(parents=True)
    config = tmp_path / "agent" / "arcagent.toml"
    config.write_text('[security]\ntier = "personal"\n')
    source = tmp_path / "source" / "skills" / "reporter"
    (source / "evals").mkdir(parents=True)
    (source / "scripts").mkdir()
    (source / "SKILL.md").write_bytes(_body("do it"))
    (source / "scripts" / "extract.py").write_bytes(b"print('x')\n")
    (source / "evals" / "test_g.py").write_text(
        "def test_a():\n    assert 1\n\ndef test_b():\n    assert 1\n", encoding="utf-8"
    )
    service = CapabilityImportService(root)
    imported = intake(tmp_path / "source", root)
    service.review(imported, target_agent_did=agent_did, limits=CapabilityImportLimits())
    operator_did = OperatorApprovalAuthority(signer).did
    service.promote(
        imported.staging_dir,
        target_agent_did=agent_did,
        operator_did=operator_did,
        signer=signer,
        config_path=config,
    )
    anchors = tmp_path / "anchors"
    resolver = AnchoredSkillRevisionResolver(
        agent_did=agent_did,
        config_path=config,
        anchor_factory=lambda did, name: FileJournalAnchor(
            anchors, scope=skill_revision_scope(did, name), signer=signer
        ),
    )
    return root / "skills" / "reporter", resolver


async def _improve(adapter: Any) -> dict[str, Any]:
    for turn in range(3):
        await adapter.observe(
            skill_name="reporter",
            tool_name="bash",
            status="ok",
            error_type=None,
            call_id=f"c{turn}",
            run_id=f"r{turn}",
        )
        await adapter.on_turn_end(turn=turn, outcome="", run_id=f"r{turn}")
    result: dict[str, Any] = await adapter.improve_now(skill_name="reporter", dry_run=False)
    return result


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fakes")
async def test_applied_improver_change_is_an_operator_signed_revision(tmp_path: Path) -> None:
    agent = AgentIdentity.generate(org="arc", agent_type="exec")
    signer = InProcessSigner(bytes(range(32)))
    operator_did = OperatorApprovalAuthority(signer).did
    folder, resolver = _promoted(tmp_path, agent.did, signer)
    reloads: list[str] = []

    async def reload() -> str:
        reloads.append("reload")
        return "reload: ok"

    _runtime.configure(
        config={"adapter": "arcskill"},
        workspace=tmp_path / "ws",
        agent_did=agent.did,
        operator_signer=signer,
        skill_revisions=resolver,
        capability_reload=reload,
    )
    await skills_ready(_Ctx(skill_registry=_Registry(_Entry("reporter", folder / "SKILL.md"))))

    result = await _improve(_runtime.state().adapter)
    assert result["status"] == "applied", result
    await asyncio.sleep(0)

    history = resolver.revision_history(folder)
    assert [(version, body) for _, version, body, _ in history] == [
        (2, _body("do it carefully").decode()),
        (1, _body("do it").decode()),
    ]
    assert result["revision"] == history[0][0]
    active = resolver.active_folder(folder)
    assert active is not None
    sidecars = list(active.rglob("*.arcsig"))
    assert sidecars
    for sidecar in sidecars:
        signature = ArtifactSignature.from_json(sidecar.read_text(encoding="utf-8"))
        assert signature is not None
        assert signature.signer_did == operator_did
        assert signature.signer_did != agent.did
        assert signature.public_key != agent.public_key.hex()
    # The installed original was never rewritten in place.
    assert (folder / "SKILL.md").read_bytes() == _body("do it")
    assert reloads == ["reload"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fakes")
async def test_without_an_anchored_authority_the_improver_refuses(tmp_path: Path) -> None:
    agent = AgentIdentity.generate(org="arc", agent_type="exec")
    signer = InProcessSigner(bytes(range(32)))
    folder, resolver = _promoted(tmp_path, agent.did, signer)
    _runtime.configure(
        config={"adapter": "arcskill"},
        workspace=tmp_path / "ws",
        agent_did=agent.did,
        operator_signer=signer,
        skill_revisions=None,
    )
    await skills_ready(_Ctx(skill_registry=_Registry(_Entry("reporter", folder / "SKILL.md"))))

    result = await _improve(_runtime.state().adapter)
    assert result["status"] == "unavailable", result
    assert resolver.revision_history(folder) == []
    assert (folder / "SKILL.md").read_bytes() == _body("do it")
