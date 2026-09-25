"""SPEC-081 RED wave — no-load-execution for script-bearing skills (REQ-403, ASI05).

T-1058: importing, reviewing, promoting, and loading a skill that CONTAINS a
script must execute ZERO guest processes. Every subprocess / async-exec entry
point is spied before the journey runs and must never be called.

RED today: the skill carries its script under a non-standard folder (``lib/``),
which the strict layout gate rejects at intake, so the rich script-bearing
skill cannot complete the import -> load journey at all. Once the layout is
loosened (T-1052) this test proves the load path only copies and verifies —
it never runs the script.
"""

from __future__ import annotations

import asyncio
import subprocess
import zipfile
from pathlib import Path

import pytest
from arctrust import InProcessSigner, generate_keypair

from arcagent.capabilities.inventory import collect_agent_capability_inventory
from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.models import CapabilityImportLimits
from arcagent.modules.capability_import.service import CapabilityImportService

_OPERATOR_DID = "did:arc:operator:alpha"
_TARGET_DID = "did:arc:agent:target"

_CONFIG = """\
[agent]
name = "no-exec-test"

[security]
tier = "federal"

[security.validators]
auto_run_agent_code = false
"""

_SKILL_MD = (
    b"---\nname: analyzer\ndescription: a script-bearing skill\n---\n"
    b"## Files\nnone\n## Contract\nnone\n## Knowledge\nnone\n"
    b"## Steps\nRun the analyzer.\n## Red Flags & Rationalizations\nnone\n"
    b"## Validation\nnone\n## Examples\nnone\n"
)

# A script that would side-effect if it ever ran; the load path must never run it.
_SCRIPT = b"print('analyzer skill script executed')\n"


def _archive(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("skills/analyzer/SKILL.md", _SKILL_MD)
        archive.writestr("skills/analyzer/lib/run.py", _SCRIPT)
    return path


def test_import_review_promote_load_of_script_skill_spawns_no_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawns: list[str] = []

    def _guard(label: str):
        def _spy(*args: object, **kwargs: object) -> object:
            spawns.append(label)
            raise AssertionError(f"guest process spawned via {label} on the import/load path")

        return _spy

    for attr in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, attr, _guard(f"subprocess.{attr}"), raising=False)
    monkeypatch.setattr("os.system", _guard("os.system"), raising=False)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _guard("asyncio.create_subprocess_exec"), raising=False
    )
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_shell",
        _guard("asyncio.create_subprocess_shell"),
        raising=False,
    )

    agent = tmp_path / "agent"
    capabilities = agent / "capabilities"
    capabilities.mkdir(parents=True)
    config = agent / "arcagent.toml"
    config.write_text(_CONFIG, encoding="utf-8")

    staged = intake(_archive(tmp_path / "import.zip"), capabilities)
    service = CapabilityImportService(capabilities)
    manifest = service.review(
        staged, target_agent_did=_TARGET_DID, limits=CapabilityImportLimits()
    )
    assert "analyzer" in manifest.skills

    service.promote(
        staged.staging_dir,
        target_agent_did=_TARGET_DID,
        operator_did=_OPERATOR_DID,
        signer=InProcessSigner(generate_keypair().private_key),
        config_path=config,
    )

    inventory = asyncio.run(collect_agent_capability_inventory(config))
    by_name = {item.name: item for item in inventory.items}
    assert by_name["analyzer"].status == "loaded", by_name["analyzer"].status_detail

    assert spawns == [], f"guest processes were spawned on the import/load path: {spawns}"
