"""SPEC-081 Phase 3 — inherited trust guards on a rich (looser-layout) skill.

T-1070 (REQ-409, COMP-006): a rich skill-creator package (SKILL.md + a deep
``knowledge/`` subtree + a script) must complete the full intake -> review ->
promote journey, be signed per-file (``.arcsig`` sidecars), and load under the
agent's real trust gate. Then, when ONE byte of the promoted, gated ``SKILL.md``
changes, the load MUST be invalidated (signature no longer verifies / TOFU sees
drift) so the skill no longer loads until it is re-reviewed and re-approved.

COMP-006 is "no new logic" — ``promote`` already signs + pins, and the load-path
Sign/TOFU gate already re-verifies on every scan. This is therefore a regression
LOCK: it proves the inherited guards hold end to end for the looser layout that
SPEC-081 newly admits. It uses a real signer/keystore fixture like the existing
promotion tests, and loads through the same ``collect_agent_capability_inventory``
seam arcui and ``arc trust`` use, so a "loaded" verdict here means a real load.
"""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

from arctrust import InProcessSigner, generate_keypair

from arcagent.capabilities.inventory import collect_agent_capability_inventory
from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.models import (
    CapabilityImportLimits,
    CapabilityImportStatus,
)
from arcagent.modules.capability_import.service import CapabilityImportService

_OPERATOR_DID = "did:arc:operator:alpha"
_TARGET_DID = "did:arc:agent:target"
_SKILL_NAME = "spec081_rich_skill"

_CONFIG = """\
[agent]
name = "rich-skill-test"

[security]
tier = "federal"

[security.validators]
auto_run_agent_code = false
"""

_SKILL_MD = (
    f"---\nname: {_SKILL_NAME}\ndescription: a rich imported skill\n---\n"
    "## Files\nnone\n"
    "## Contract\nnone\n"
    "## Knowledge\nnone\n"
    "## Steps\nUse it.\n"
    "## Red Flags & Rationalizations\nnone\n"
    "## Validation\nnone\n"
    "## Examples\nnone\n"
).encode()

_KNOWLEDGE_DOC = b"# deep knowledge\n\nA nested note under knowledge/a/.\n"
_SCRIPT = b"import sys\n\nprint('rich skill script', file=sys.stderr)\n"


def _rich_archive(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"skills/{_SKILL_NAME}/SKILL.md", _SKILL_MD)
        archive.writestr(f"skills/{_SKILL_NAME}/knowledge/a/b.md", _KNOWLEDGE_DOC)
        archive.writestr(f"skills/{_SKILL_NAME}/scripts/run.py", _SCRIPT)
    return path


def _setup(tmp_path: Path):
    agent = tmp_path / "agent"
    capabilities = agent / "capabilities"
    capabilities.mkdir(parents=True)
    config = agent / "arcagent.toml"
    config.write_text(_CONFIG, encoding="utf-8")
    staged = intake(_rich_archive(tmp_path / "rich.zip"), capabilities)
    service = CapabilityImportService(capabilities)
    manifest = service.review(
        staged,
        target_agent_did=_TARGET_DID,
        limits=CapabilityImportLimits(),
    )
    return agent, config, staged.staging_dir, service, manifest


def _skill_status(config: Path) -> str:
    inventory = asyncio.run(collect_agent_capability_inventory(config))
    for item in inventory.items:
        if item.kind == "skill" and item.name == _SKILL_NAME:
            return item.status
    return "absent"


def test_rich_skill_promotes_signs_and_loads_then_one_byte_break_invalidates(
    tmp_path: Path,
) -> None:
    agent, config, staging, service, manifest = _setup(tmp_path)
    key = generate_keypair()

    promoted = service.promote(
        staging,
        target_agent_did=_TARGET_DID,
        operator_did=_OPERATOR_DID,
        signer=InProcessSigner(key.private_key),
        config_path=config,
    )

    caps = agent / "capabilities"
    # (a) The full looser-layout tree promoted, and every promoted file is signed.
    promoted_relative = {path.relative_to(caps).as_posix() for path in promoted}
    assert promoted_relative == {
        f"skills/{_SKILL_NAME}/SKILL.md",
        f"skills/{_SKILL_NAME}/knowledge/a/b.md",
        f"skills/{_SKILL_NAME}/scripts/run.py",
    }
    for path in promoted:
        assert path.with_name(path.name + ".arcsig").is_file(), f"missing signature for {path}"
    assert service.status(manifest, staging) is CapabilityImportStatus.PROMOTED

    # ...and it actually LOADS under the agent's real (federal) trust gate.
    assert _skill_status(config) == "loaded"

    # (b) Mutate ONE byte of the promoted, gated SKILL.md.
    skill_md = caps / "skills" / _SKILL_NAME / "SKILL.md"
    original = skill_md.read_bytes()
    skill_md.write_bytes(original + b" ")

    # The signature no longer verifies / TOFU sees drift: the skill is invalidated
    # and no longer loads until re-reviewed and re-approved (REQ-409).
    status_after = _skill_status(config)
    assert status_after != "loaded", (
        "REQ-409 violated: a one-byte change to a promoted skill still loaded "
        f"(status {status_after!r})"
    )
