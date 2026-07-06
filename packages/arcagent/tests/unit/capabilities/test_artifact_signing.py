"""SPEC-033 D2 — agent-authored artifacts are signed on write.

The self-modification tools (`create_tool`, `create_skill`, `update_tool`,
`update_skill`) write a detached `.arcsig` sidecar over the artifact bytes with
the agent's own DID key. The loader re-verifies that sidecar at load.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust.identity import AgentIdentity

from arcagent.builtins.capabilities import _runtime
from arcagent.capabilities import artifact_signing
from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


@pytest.fixture
def identity() -> AgentIdentity:
    return AgentIdentity.generate(org="blackarc", agent_type="executor")


@pytest.fixture
def configured(tmp_path: Path, identity: AgentIdentity) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "capabilities").mkdir()
    reg = CapabilityRegistry()
    loader = CapabilityLoader(
        scan_roots=[("workspace", workspace / "capabilities")],
        registry=reg,
    )
    _runtime.configure(workspace=workspace, loader=loader, identity=identity)
    return workspace


def test_sidecar_path_convention() -> None:
    assert artifact_signing.sidecar_path(Path("/x/hello.py")) == Path("/x/hello.py.arcsig")


def test_verify_file_true_for_valid_sidecar(
    tmp_path: Path, identity: AgentIdentity
) -> None:
    artifact = tmp_path / "hello.py"
    content = b"async def fn(): return 1\n"
    artifact.write_bytes(content)
    artifact_signing.write_signature(
        artifact, content, signer_did=identity.did, private_key=identity.signing_seed
    )
    assert artifact_signing.verify_file(artifact, content) is True
    # Pin to the signer's key.
    assert artifact_signing.verify_file(
        artifact, content, trusted_public_key=identity.public_key
    ) is True


def test_verify_file_false_when_unsigned(tmp_path: Path) -> None:
    artifact = tmp_path / "hello.py"
    artifact.write_bytes(b"x")
    assert artifact_signing.verify_file(artifact, b"x") is False


def test_verify_file_false_when_tampered(tmp_path: Path, identity: AgentIdentity) -> None:
    artifact = tmp_path / "hello.py"
    original = b"async def fn(): return 1\n"
    artifact.write_bytes(original)
    artifact_signing.write_signature(
        artifact, original, signer_did=identity.did, private_key=identity.signing_seed
    )
    # Bytes changed after signing — sidecar no longer matches.
    assert artifact_signing.verify_file(artifact, original + b"# evil\n") is False


@pytest.mark.asyncio
async def test_create_tool_writes_valid_signature(configured: Path) -> None:
    from arcagent.builtins.capabilities.create_tool import create_tool

    source = (
        "from arcagent.tools._decorator import tool\n"
        "@tool(description='greet', version='1.0.0')\n"
        "async def hello() -> str:\n"
        "    return 'hi'\n"
    )
    await create_tool(name="hello", source=source)
    target = configured / "capabilities" / "hello.py"
    assert artifact_signing.verify_file(target, target.read_bytes()) is True


@pytest.mark.asyncio
async def test_create_skill_writes_valid_signature(configured: Path) -> None:
    from arcagent.builtins.capabilities.create_skill import create_skill

    await create_skill(name="my-skill", description="x", triggers=["t"], tools=["read"])
    skill_md = configured / "capabilities/skills/my-skill/SKILL.md"
    assert artifact_signing.verify_file(skill_md, skill_md.read_bytes()) is True


@pytest.mark.asyncio
async def test_no_identity_writes_no_sidecar(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "capabilities").mkdir(parents=True)
    _runtime.configure(workspace=workspace, loader=None, identity=None)
    from arcagent.builtins.capabilities.create_tool import create_tool

    source = (
        "from arcagent.tools._decorator import tool\n"
        "@tool(description='x', version='1.0.0')\n"
        "async def hello() -> str:\n    return 'hi'\n"
    )
    await create_tool(name="hello", source=source)
    target = workspace / "capabilities" / "hello.py"
    assert target.exists()
    assert not artifact_signing.sidecar_path(target).exists()
