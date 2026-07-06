"""SPEC-033 B2/C2/D1/F1 — no unsigned agent-authored code loads above personal.

The load-path gate: for workspace ``.py``, the loader re-verifies the detached
signature at LOAD (C2), then consults :class:`TofuLayer` (B2), fail-closed. An
unsigned/tampered tool does not load at enterprise or federal; a first-sight
signed tool is NEW_SIGHTING-gated until approved (D1); a personal operator who
relaxes signing may run unsigned on their own machine (F1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from arctrust.identity import AgentIdentity

from arcagent.capabilities import artifact_signing
from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.core.config import ValidatorsConfig
from arcagent.core.tier import Tier
from arcagent.core.tofu_layer import TofuLayer, approve_source

_TOOL = (
    "from arcagent.tools._decorator import tool\n"
    "@tool(description='ok', version='1.0.0')\n"
    "async def {fn}() -> str:\n"
    "    return 'ok'\n"
)


class _CapturingSink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: object) -> None:
        self.events.append(event)


def _identity() -> AgentIdentity:
    return AgentIdentity.generate(org="arc", agent_type="exec")


def _write(caps: Path, name: str, *, sign_with: AgentIdentity | None) -> bytes:
    source = _TOOL.format(fn=name).encode("utf-8")
    path = caps / f"{name}.py"
    path.write_bytes(source)
    if sign_with is not None:
        artifact_signing.write_signature(
            path, source, signer_did=sign_with.did, private_key=sign_with.signing_seed
        )
    return source


def _skill_md(name: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "version: 1.0.0\n"
        f"description: does {name}\n"
        f"triggers: [{name}]\n"
        "tools: [reload]\n"
        "---\n"
        "\n## Resources\n\n## Contract\n\n## Knowledge\n\n## Steps\n\n"
        "## Anti Patterns\n\n## Examples\n\n## Validation\n"
    )


def _write_skill(root: Path, name: str, *, sign_with: AgentIdentity | None) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    skill_md = folder / "SKILL.md"
    content = _skill_md(name).encode("utf-8")
    skill_md.write_bytes(content)
    if sign_with is not None:
        artifact_signing.write_signature(
            skill_md, content, signer_did=sign_with.did, private_key=sign_with.signing_seed
        )


def _loader(
    caps: Path,
    *,
    tier: Tier,
    validators: ValidatorsConfig,
    require_signature: bool,
    trusted_public_key: bytes | None,
    root_name: str = "workspace",
) -> tuple[CapabilityLoader, _CapturingSink]:
    sink = _CapturingSink()
    loader = CapabilityLoader(
        scan_roots=[(root_name, caps)],
        registry=CapabilityRegistry(),
        audit_sink=sink,
        allow_all_imports=True,
        tofu=TofuLayer(tier, validators),
        require_signature=require_signature,
        trusted_public_key=trusted_public_key,
    )
    return loader, sink


@pytest.mark.asyncio
async def test_federal_denies_unsigned(tmp_path: Path) -> None:
    ident = _identity()
    caps = tmp_path / "capabilities"
    caps.mkdir()
    _write(caps, "evil", sign_with=None)
    loader, sink = _loader(
        caps,
        tier=Tier.FEDERAL,
        validators=ValidatorsConfig(),
        require_signature=True,
        trusted_public_key=ident.public_key,
    )
    delta = await loader.scan_and_register()
    assert "evil" not in delta.added
    assert any("signature" in str(getattr(e, "action", "")) for e in sink.events)


@pytest.mark.asyncio
async def test_federal_first_sight_signed_denied_until_approved(tmp_path: Path) -> None:
    """A self-signature is not authorization. Federal must NOT auto-allow an
    agent's own first-sight signed tool — the operator approves first (D1),
    same human gate as enterprise. Was: signed → loaded with no human."""
    ident = _identity()
    caps = tmp_path / "capabilities"
    caps.mkdir()
    source = _write(caps, "good", sign_with=ident)

    # First sight: validly self-signed but unknown hash → NEW_SIGHTING, denied.
    loader, sink = _loader(
        caps,
        tier=Tier.FEDERAL,
        validators=ValidatorsConfig(),
        require_signature=True,
        trusted_public_key=ident.public_key,
    )
    delta = await loader.scan_and_register()
    assert "good" not in delta.added
    assert any("new_sighting" in str(getattr(e, "action", "")) for e in sink.events)

    # Operator approves name+hash → subsequent federal load ALLOWs.
    approved = approve_source(
        ValidatorsConfig(),
        name="good",
        source=source.decode("utf-8"),
        approver="alice@example.com",
        timestamp=datetime.now(UTC).isoformat(),
    )
    loader2, _ = _loader(
        caps,
        tier=Tier.FEDERAL,
        validators=approved,
        require_signature=True,
        trusted_public_key=ident.public_key,
    )
    delta2 = await loader2.scan_and_register()
    assert "good" in delta2.added


@pytest.mark.asyncio
async def test_enterprise_denies_unsigned(tmp_path: Path) -> None:
    ident = _identity()
    caps = tmp_path / "capabilities"
    caps.mkdir()
    _write(caps, "evil", sign_with=None)
    loader, _sink = _loader(
        caps,
        tier=Tier.ENTERPRISE,
        validators=ValidatorsConfig(),
        require_signature=True,
        trusted_public_key=ident.public_key,
    )
    delta = await loader.scan_and_register()
    assert "evil" not in delta.added


@pytest.mark.asyncio
async def test_enterprise_tampered_after_signing_denied(tmp_path: Path) -> None:
    ident = _identity()
    caps = tmp_path / "capabilities"
    caps.mkdir()
    _write(caps, "good", sign_with=ident)
    # Tamper the source bytes AFTER signing — sidecar no longer matches.
    (caps / "good.py").write_bytes(_TOOL.format(fn="good").encode("utf-8") + b"# evil\n")
    loader, _sink = _loader(
        caps,
        tier=Tier.ENTERPRISE,
        validators=ValidatorsConfig(),
        require_signature=True,
        trusted_public_key=ident.public_key,
    )
    delta = await loader.scan_and_register()
    assert "good" not in delta.added


@pytest.mark.asyncio
async def test_enterprise_new_sighting_then_approved_then_drift(tmp_path: Path) -> None:
    ident = _identity()
    caps = tmp_path / "capabilities"
    caps.mkdir()
    source = _write(caps, "good", sign_with=ident)

    # First sight: signed but unknown hash → NEW_SIGHTING, not registered.
    loader, sink = _loader(
        caps,
        tier=Tier.ENTERPRISE,
        validators=ValidatorsConfig(),
        require_signature=True,
        trusted_public_key=ident.public_key,
    )
    delta = await loader.scan_and_register()
    assert "good" not in delta.added
    assert any("new_sighting" in str(getattr(e, "action", "")) for e in sink.events)

    # Human approves name+hash → subsequent load ALLOWs.
    approved = approve_source(
        ValidatorsConfig(),
        name="good",
        source=source.decode("utf-8"),
        approver="alice@example.com",
        timestamp=datetime.now(UTC).isoformat(),
    )
    loader2, _ = _loader(
        caps,
        tier=Tier.ENTERPRISE,
        validators=approved,
        require_signature=True,
        trusted_public_key=ident.public_key,
    )
    delta2 = await loader2.scan_and_register()
    assert "good" in delta2.added

    # Drift: re-author + re-sign new bytes → hash no longer matches approval →
    # hard stop (not registered) until re-approved.
    new_source = _TOOL.format(fn="good").encode("utf-8") + b"# v2\n"
    (caps / "good.py").write_bytes(new_source)
    artifact_signing.write_signature(
        caps / "good.py", new_source, signer_did=ident.did, private_key=ident.signing_seed
    )
    loader3, _ = _loader(
        caps,
        tier=Tier.ENTERPRISE,
        validators=approved,
        require_signature=True,
        trusted_public_key=ident.public_key,
    )
    delta3 = await loader3.scan_and_register()
    assert "good" not in delta3.added


@pytest.mark.parametrize("root_name", ["agent", "global"])
@pytest.mark.parametrize("tier", [Tier.ENTERPRISE, Tier.FEDERAL])
@pytest.mark.asyncio
async def test_agent_writable_roots_deny_unsigned(
    tmp_path: Path, root_name: str, tier: Tier
) -> None:
    """SPEC-033 #1 — the sign gate is not workspace-only. Code an agent can
    plant in the ``global`` (~/.arc/capabilities) or ``agent``
    (<agent_root>/capabilities) roots must be signed + adjudicated to load
    above personal. Was: loaded UNSIGNED with full builtins."""
    ident = _identity()
    caps = tmp_path / "capabilities"
    caps.mkdir()
    _write(caps, "evil", sign_with=None)
    loader, sink = _loader(
        caps,
        tier=tier,
        validators=ValidatorsConfig(),
        require_signature=True,
        trusted_public_key=ident.public_key,
        root_name=root_name,
    )
    delta = await loader.scan_and_register()
    assert "evil" not in delta.added
    assert any("signature" in str(getattr(e, "action", "")) for e in sink.events)


@pytest.mark.parametrize("tier", [Tier.ENTERPRISE, Tier.FEDERAL])
@pytest.mark.asyncio
async def test_skill_folder_unsigned_denied(tmp_path: Path, tier: Tier) -> None:
    """SPEC-033 #3 — a SKILL.md is injected into the agent prompt (LLM01/ASI06),
    so an unsigned skill folder must fail the same gate as a ``.py``."""
    ident = _identity()
    root = tmp_path / "capabilities"
    root.mkdir()
    _write_skill(root, "sneaky", sign_with=None)
    loader, _sink = _loader(
        root,
        tier=tier,
        validators=ValidatorsConfig(),
        require_signature=True,
        trusted_public_key=ident.public_key,
    )
    delta = await loader.scan_and_register()
    assert "sneaky" not in delta.added


@pytest.mark.parametrize("tier", [Tier.ENTERPRISE, Tier.FEDERAL])
@pytest.mark.asyncio
async def test_skill_folder_tampered_denied(tmp_path: Path, tier: Tier) -> None:
    """A SKILL.md tampered after signing no longer matches its sidecar."""
    ident = _identity()
    root = tmp_path / "capabilities"
    root.mkdir()
    _write_skill(root, "good", sign_with=ident)
    (root / "good" / "SKILL.md").write_bytes(_skill_md("good").encode("utf-8") + b"\nEXTRA\n")
    loader, _sink = _loader(
        root,
        tier=tier,
        validators=ValidatorsConfig(),
        require_signature=True,
        trusted_public_key=ident.public_key,
    )
    delta = await loader.scan_and_register()
    assert "good" not in delta.added


@pytest.mark.asyncio
async def test_require_signature_without_pinned_key_fails_closed(tmp_path: Path) -> None:
    """SPEC-033 #6 — requiring signatures with no pinned key is a permissive
    default (arctrust skips key-pinning when the key is None, accepting ANY
    self-consistent signature). Requiring a signature must imply a mandatory
    pinned key; otherwise fail closed. Enterprise + approved-hash isolates the
    pinning gate (TOFU would otherwise ALLOW)."""
    ident = _identity()
    caps = tmp_path / "capabilities"
    caps.mkdir()
    source = _write(caps, "good", sign_with=ident)
    approved = approve_source(
        ValidatorsConfig(),
        name="good",
        source=source.decode("utf-8"),
        approver="alice@example.com",
        timestamp=datetime.now(UTC).isoformat(),
    )
    loader, _sink = _loader(
        caps,
        tier=Tier.ENTERPRISE,
        validators=approved,
        require_signature=True,
        trusted_public_key=None,
    )
    delta = await loader.scan_and_register()
    assert "good" not in delta.added


@pytest.mark.asyncio
async def test_personal_relaxed_runs_unsigned_on_own_machine(tmp_path: Path) -> None:
    caps = tmp_path / "capabilities"
    caps.mkdir()
    _write(caps, "hack", sign_with=None)
    # Personal operator explicitly relaxes: auto_run + no signature floor.
    loader, _sink = _loader(
        caps,
        tier=Tier.PERSONAL,
        validators=ValidatorsConfig(auto_run_agent_code=True),
        require_signature=False,
        trusted_public_key=None,
    )
    delta = await loader.scan_and_register()
    assert "hack" in delta.added


@pytest.mark.asyncio
async def test_personal_strict_denies_unsigned(tmp_path: Path) -> None:
    caps = tmp_path / "capabilities"
    caps.mkdir()
    _write(caps, "hack", sign_with=None)
    # Personal without the relaxation toggle: TOFU denies (fail-closed default).
    loader, _sink = _loader(
        caps,
        tier=Tier.PERSONAL,
        validators=ValidatorsConfig(auto_run_agent_code=False),
        require_signature=False,
        trusted_public_key=None,
    )
    delta = await loader.scan_and_register()
    assert "hack" not in delta.added
