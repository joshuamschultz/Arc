"""Externally anchored skill revisions preserve signed bundle trust."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from arctrust import AnchorHead, InProcessSigner, TofuLayer, approve, load_validators

import arcagent
from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry, SkillEntry
from arcagent.capabilities.capability_signing import sign
from arcagent.capabilities.provider import AgentCapabilityProvider, _Skill
from arcagent.capabilities.skill_validator import _parse_skill_md
from arcagent.modules.capability_import import revisions
from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.models import CapabilityImportLimits
from arcagent.modules.capability_import.revisions import (
    AnchoredSkillRevisionResolver,
    ReviewedSkillBundle,
    reviewed_bundle_digest,
)
from arcagent.modules.capability_import.service import CapabilityImportService


def test_standalone_agent_import_survives_removed_revision_leaf(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[3] / "src" / "arcagent"
    isolated = tmp_path / "isolated"
    shutil.copytree(source, isolated / "arcagent", ignore=shutil.ignore_patterns("__pycache__"))
    (isolated / "arcagent" / "modules" / "capability_import" / "revisions.py").unlink()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import arcagent; loader = arcagent.CapabilityLoader(scan_roots=[], "
            "registry=arcagent.CapabilityRegistry()); "
            "assert hasattr(arcagent, 'SkillArtifactResolver'); "
            "assert loader._skill_artifact_resolver is not None; print(arcagent.__file__)",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(isolated)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert str(isolated) in result.stdout


def test_optional_revision_export_is_lazy_and_public() -> None:
    assert arcagent.AnchoredSkillRevisionResolver is AnchoredSkillRevisionResolver
    assert arcagent.ReviewedSkillBundle is ReviewedSkillBundle


def _body(step: str) -> bytes:
    return (
        "---\nname: reporter\ndescription: Create reports\n---\n"
        "## Resources\nnone\n## Contract\nfollow the steps\n"
        "## Knowledge\nsource data\n## Steps\n"
        f"{step}\n"
        "## Anti Patterns\nnone\n## Examples\nexample\n## Validation\ncheck\n"
    ).encode()


class _Anchor:
    def __init__(self, scope: str) -> None:
        self.scope = scope
        self.head: AnchorHead | None = None

    def latest(self) -> AnchorHead | None:
        return self.head

    def compare_and_advance(
        self, expected: AnchorHead | None, digest: str, intent: str
    ) -> AnchorHead:
        if expected != self.head:
            raise ValueError("stale anchor")
        self.head = AnchorHead(
            scope=self.scope,
            version=expected.version + 1 if expected else 1,
            digest=digest,
            previous_digest=expected.digest if expected else None,
            intent=intent,
        )
        return self.head


@pytest.mark.asyncio
async def test_reviewed_bundle_promotion_reloads_exact_active_content(tmp_path: Path) -> None:
    resolver, folder, anchor, signer = _installed(tmp_path)
    resolver.revise(
        folder,
        _body("first"),
        expected_sha256=hashlib.sha256(_body("old")).hexdigest(),
        signer=signer,
        operator_did="did:arc:operator:test",
    )
    assert anchor.head is not None
    registry = CapabilityRegistry()
    loader = CapabilityLoader(
        scan_roots=[("workspace-skills", folder.parent)],
        registry=registry,
        skill_artifact_resolver=resolver,
        tofu=TofuLayer("federal", load_validators(tmp_path / "arcagent.toml")),
        require_signature=True,
        trusted_public_keys=(signer.public_key,),
    )
    await loader.reload()

    class Runtime:
        @property
        def skills(self) -> Sequence[SkillEntry]:
            return registry.skill_entries()

        async def reload_or_raise(self) -> object:
            return await loader.reload()

    files = {"SKILL.md": _body("reviewed"), "references/schema.txt": b"reviewed schema"}
    proposal = ReviewedSkillBundle(files, reviewed_bundle_digest(files), anchor.head.digest)
    activated_digest = await resolver.promote_reviewed_bundle(
        folder,
        proposal,
        signer=signer,
        operator_did="did:arc:operator:test",
        runtime=Runtime(),
    )
    active = resolver.read_bundle(folder)
    assert active["SKILL.md"] == files["SKILL.md"]
    assert active["references/schema.txt"] == files["references/schema.txt"]
    entry = await registry.get_skill("reporter")
    assert entry is not None and entry.read_current is not None
    assert entry.read_current() == _body("reviewed").decode()
    assert anchor.head.version == 2
    assert activated_digest == anchor.head.digest


@pytest.mark.asyncio
async def test_reviewed_bundle_promotion_refuses_stale_or_changed_review(tmp_path: Path) -> None:
    resolver, folder, anchor, signer = _installed(tmp_path)
    resolver.revise(
        folder,
        _body("first"),
        expected_sha256=hashlib.sha256(_body("old")).hexdigest(),
        signer=signer,
        operator_did="did:arc:operator:test",
    )
    assert anchor.head is not None
    original_head = anchor.head
    files = {"SKILL.md": _body("reviewed"), "references/schema.txt": b"reviewed schema"}

    class Runtime:
        @property
        def skills(self) -> Sequence[SkillEntry]:
            return ()

        async def reload_or_raise(self) -> object:
            raise AssertionError("runtime reload must not occur")

    for proposal in (
        ReviewedSkillBundle(files, "0" * 64, original_head.digest),
        ReviewedSkillBundle(files, reviewed_bundle_digest(files), "0" * 64),
        ReviewedSkillBundle(
            {**files, "references/../assets/escape.txt": b"bad"},
            reviewed_bundle_digest({**files, "references/../assets/escape.txt": b"bad"}),
            original_head.digest,
        ),
    ):
        with pytest.raises(ValueError):
            await resolver.promote_reviewed_bundle(
                folder,
                proposal,
                signer=signer,
                operator_did="did:arc:operator:test",
                runtime=Runtime(),
            )
    assert anchor.head == original_head


@pytest.mark.asyncio
async def test_reviewed_bundle_promotion_does_not_report_unloaded_runtime(tmp_path: Path) -> None:
    resolver, folder, anchor, signer = _installed(tmp_path)
    resolver.revise(
        folder,
        _body("first"),
        expected_sha256=hashlib.sha256(_body("old")).hexdigest(),
        signer=signer,
        operator_did="did:arc:operator:test",
    )
    assert anchor.head is not None
    files = {"SKILL.md": _body("reviewed"), "references/schema.txt": b"reviewed schema"}

    class Runtime:
        @property
        def skills(self) -> Sequence[SkillEntry]:
            return ()

        async def reload_or_raise(self) -> object:
            return "no live registration"

    with pytest.raises(ValueError, match="runtime did not load"):
        await resolver.promote_reviewed_bundle(
            folder,
            ReviewedSkillBundle(files, reviewed_bundle_digest(files), anchor.head.digest),
            signer=signer,
            operator_did="did:arc:operator:test",
            runtime=Runtime(),
        )


def _installed(
    tmp_path: Path,
) -> tuple[AnchoredSkillRevisionResolver, Path, _Anchor, InProcessSigner]:
    root = tmp_path / "capabilities"
    folder = root / "skills" / "reporter"
    folder.mkdir(parents=True)
    skill = folder / "SKILL.md"
    skill.write_bytes(_body("old"))
    (folder / "references").mkdir()
    (folder / "references" / "schema.txt").write_text("data source", encoding="utf-8")
    config = tmp_path / "arcagent.toml"
    config.write_text('[security]\ntier = "federal"\n')
    signer = InProcessSigner(bytes(range(32)))
    sign(skill, signer_did="did:arc:operator:test", signer=signer, config_path=config)
    sign(
        folder / "references" / "schema.txt",
        signer_did="did:arc:operator:test",
        signer=signer,
        config_path=config,
    )
    approve(
        config,
        name="skill-resource/reporter/references/schema.txt",
        source="data source",
        approver="did:arc:operator:test",
        timestamp="2026-09-23T00:00:00Z",
    )
    did = "did:arc:agent:reporter"
    anchor = _Anchor(f"skill/{hashlib.sha256(did.encode()).hexdigest()}/reporter")
    resolver = AnchoredSkillRevisionResolver(
        agent_did=did, config_path=config, anchor_factory=lambda _did, _name: anchor
    )
    return resolver, folder, anchor, signer


@pytest.mark.parametrize("change", ["tamper", "add"])
def test_first_revision_refuses_unreviewed_resource(tmp_path: Path, change: str) -> None:
    resolver, folder, anchor, signer = _installed(tmp_path)
    if change == "tamper":
        (folder / "references" / "schema.txt").write_text("malicious", encoding="utf-8")
    else:
        (folder / "references" / "inject.sh").write_text("echo malicious", encoding="utf-8")
    with pytest.raises(ValueError, match="trusted"):
        resolver.revise(
            folder,
            _body("new"),
            expected_sha256=hashlib.sha256(_body("old")).hexdigest(),
            signer=signer,
            operator_did="did:arc:operator:test",
        )
    assert anchor.head is None


def test_import_preserves_signed_binary_resource_through_revision(tmp_path: Path) -> None:
    source = tmp_path / "source"
    folder = source / "skills" / "reporter"
    (folder / "assets").mkdir(parents=True)
    (folder / "SKILL.md").write_bytes(_body("old"))
    binary = b"%PDF-1.7\n\x00\xff\xfe\n"
    (folder / "assets" / "plot.pdf").write_bytes(binary)
    root = tmp_path / "agent" / "capabilities"
    imported = intake(source, root)
    service = CapabilityImportService(root)
    service.review(
        imported,
        target_agent_did="did:arc:agent:reporter",
        limits=CapabilityImportLimits(),
    )
    config = tmp_path / "arcagent.toml"
    config.write_text('[security]\ntier = "federal"\n')
    signer = InProcessSigner(bytes(range(32)))
    service.promote(
        imported.staging_dir,
        target_agent_did="did:arc:agent:reporter",
        operator_did="did:arc:operator:test",
        signer=signer,
        config_path=config,
    )
    promoted = root / "skills" / "reporter"
    assert (promoted / "assets" / "plot.pdf").read_bytes() == binary
    anchor = _Anchor(
        "skill/" + hashlib.sha256(b"did:arc:agent:reporter").hexdigest() + "/reporter"
    )
    resolver = AnchoredSkillRevisionResolver(
        agent_did="did:arc:agent:reporter",
        config_path=config,
        anchor_factory=lambda _did, _name: anchor,
    )
    resolver.revise(
        promoted,
        _body("new"),
        expected_sha256=hashlib.sha256(_body("old")).hexdigest(),
        signer=signer,
        operator_did="did:arc:operator:test",
    )
    active = resolver.resolve(promoted, "workspace-skills")
    assert active is not None
    assert (active.parent / "assets" / "plot.pdf").read_bytes() == binary


def test_revision_is_anchored_and_revalidated_on_use(tmp_path: Path) -> None:
    resolver, folder, anchor, signer = _installed(tmp_path)
    expected = hashlib.sha256((folder / "SKILL.md").read_bytes()).hexdigest()
    digest = resolver.revise(
        folder,
        _body("new"),
        expected_sha256=expected,
        signer=signer,
        operator_did="did:arc:operator:test",
    )
    assert digest == hashlib.sha256(_body("new")).hexdigest()
    assert anchor.head is not None and anchor.head.version == 1
    path = resolver.resolve(folder, "workspace-skills")
    assert path is not None
    assert resolver.read_current(folder, path) == _body("new").decode()
    assert (path.parent / "references" / "schema.txt").read_text() == "data source"
    (path.parent / "references" / "schema.txt").write_text("tampered")
    with pytest.raises(ValueError, match="bundle changed"):
        resolver.read_current(folder, path)


def test_prior_version_activation_advances_anchor_and_keeps_history(tmp_path: Path) -> None:
    resolver, folder, anchor, signer = _installed(tmp_path)
    original = hashlib.sha256(_body("old")).hexdigest()
    resolver.revise(
        folder,
        _body("first"),
        expected_sha256=original,
        signer=signer,
        operator_did="did:arc:operator:test",
    )
    first = anchor.head
    assert first is not None
    resolver.revise(
        folder,
        _body("second"),
        expected_sha256=hashlib.sha256(_body("first")).hexdigest(),
        signer=signer,
        operator_did="did:arc:operator:test",
        resource_updates={"references/schema.txt": b"updated schema"},
    )
    second = anchor.head
    assert second is not None
    assert [version for _, version, _, _ in resolver.revision_history(folder)] == [2, 1]
    resolver.activate_prior(
        folder,
        first.digest,
        expected_sha256=hashlib.sha256(_body("second")).hexdigest(),
        signer=signer,
        operator_did="did:arc:operator:test",
    )
    assert anchor.head is not None
    assert anchor.head.version == 3
    assert anchor.head.previous_digest == second.digest
    assert (
        resolver.read_current(folder, resolver.resolve(folder, "workspace-skills"))
        == _body("first").decode()
    )
    active = resolver.resolve(folder, "workspace-skills")
    assert (active.parent / "references" / "schema.txt").read_bytes() == b"data source"
    assert [version for _, version, _, _ in resolver.revision_history(folder)] == [3, 2, 1]


def test_anchored_resolver_refuses_empty_and_reset_head_in_fresh_instance(tmp_path: Path) -> None:
    resolver, folder, anchor, signer = _installed(tmp_path)
    with pytest.raises(ValueError, match="unavailable"):
        resolver.resolve(folder, "agent-skills")
    resolver.revise(
        folder,
        _body("first"),
        expected_sha256=hashlib.sha256(_body("old")).hexdigest(),
        signer=signer,
        operator_did="did:arc:operator:test",
    )
    anchor.head = None
    fresh = AnchoredSkillRevisionResolver(
        agent_did="did:arc:agent:reporter",
        config_path=tmp_path / "arcagent.toml",
        anchor_factory=lambda _did, _name: anchor,
    )
    with pytest.raises(ValueError, match="unavailable"):
        fresh.resolve(folder, "agent-skills")


def test_fresh_resolver_refuses_older_head_with_signed_newer_evidence(tmp_path: Path) -> None:
    resolver, folder, anchor, signer = _installed(tmp_path)
    resolver.revise(
        folder,
        _body("first"),
        expected_sha256=hashlib.sha256(_body("old")).hexdigest(),
        signer=signer,
        operator_did="did:arc:operator:test",
    )
    older = anchor.head
    resolver.revise(
        folder,
        _body("second"),
        expected_sha256=hashlib.sha256(_body("first")).hexdigest(),
        signer=signer,
        operator_did="did:arc:operator:test",
    )
    anchor.head = older
    fresh = AnchoredSkillRevisionResolver(
        agent_did="did:arc:agent:reporter",
        config_path=tmp_path / "arcagent.toml",
        anchor_factory=lambda _did, _name: anchor,
    )
    with pytest.raises(ValueError, match="regressed"):
        fresh.resolve(folder, "agent-skills")


def test_invalid_and_stale_revision_do_not_advance(tmp_path: Path) -> None:
    resolver, folder, anchor, signer = _installed(tmp_path)
    expected = hashlib.sha256((folder / "SKILL.md").read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="failed validation"):
        resolver.revise(
            folder,
            b"invalid",
            expected_sha256=expected,
            signer=signer,
            operator_did="did:arc:operator:test",
        )
    assert anchor.head is None
    resolver.revise(
        folder,
        _body("new"),
        expected_sha256=expected,
        signer=signer,
        operator_did="did:arc:operator:test",
    )
    with pytest.raises(ValueError, match="changed since it was opened"):
        resolver.revise(
            folder,
            _body("stale"),
            expected_sha256=expected,
            signer=signer,
            operator_did="did:arc:operator:test",
        )


def test_snapshot_refuses_fifo_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.mkfifo(tmp_path / "pipe")
    real_open = os.open

    def guarded_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int, **kwargs: int
    ) -> int:
        if path == "pipe":
            assert flags & os.O_NONBLOCK
        return real_open(path, flags, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    with pytest.raises(ValueError, match="special file"):
        revisions._snapshot(tmp_path)


def test_snapshot_bounds_empty_directories(tmp_path: Path) -> None:
    for number in range(513):
        (tmp_path / f"dir-{number}").mkdir()
    with pytest.raises(ValueError, match="resource limits"):
        revisions._snapshot(tmp_path)


def test_snapshot_bounds_directory_depth(tmp_path: Path) -> None:
    current = tmp_path
    for number in range(20):
        current /= f"dir-{number}"
        current.mkdir()
    with pytest.raises(ValueError, match="resource limits"):
        revisions._snapshot(tmp_path)


def test_skill_frontmatter_is_bounded_before_yaml_parse() -> None:
    text = "---\nname: reporter\ndescription: " + ("x" * 17_000) + "\n---\nbody"
    with pytest.raises(ValueError, match="frontmatter exceeds"):
        _parse_skill_md(text)


@pytest.mark.asyncio
async def test_anchored_revision_loads_and_provider_reads_verified_body(tmp_path: Path) -> None:
    resolver, folder, anchor, signer = _installed(tmp_path)
    resolver.revise(
        folder,
        _body("new"),
        expected_sha256=hashlib.sha256(_body("old")).hexdigest(),
        signer=signer,
        operator_did="did:arc:operator:test",
    )
    registry = CapabilityRegistry()
    loader = CapabilityLoader(
        scan_roots=[("workspace-skills", folder.parent)],
        registry=registry,
        skill_artifact_resolver=resolver,
        tofu=TofuLayer("federal", load_validators(tmp_path / "arcagent.toml")),
        require_signature=True,
        trusted_public_keys=(signer.public_key,),
    )
    outcome = await loader.reload()
    entry = await registry.get_skill("reporter")
    assert entry is not None, outcome
    assert entry.read_current is not None
    audit_events: list[tuple[str, dict[str, object]]] = []
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
        caller_did="did:arc:agent:reporter",
        audit=lambda event, details: audit_events.append((event, details)),
    )
    assert (
        await provider.load("reporter", caller_did="did:arc:agent:reporter")
        == _body("new").decode()
    )
    entry.location.write_text("tampered", encoding="utf-8")
    assert await provider.load("reporter", caller_did="did:arc:agent:reporter") is None
    assert audit_events[-1][0] == "skill.load"
    assert audit_events[-1][1]["outcome"] == "denied"
    assert audit_events[-1][1]["actor_did"] == "did:arc:agent:reporter"
    assert anchor.head is not None
    anchor.head = AnchorHead(
        scope=anchor.scope,
        version=anchor.head.version + 1,
        digest="0" * 64,
        previous_digest=anchor.head.digest,
    )
    assert await provider.load("reporter", caller_did="did:arc:agent:reporter") is None
    assert audit_events[-1][1]["outcome"] == "denied"
