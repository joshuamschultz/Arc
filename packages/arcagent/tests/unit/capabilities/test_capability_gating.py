"""SPEC-021 — gated-capability discovery + approval round-trip.

``arcagent.capabilities.inventory.list_gated`` DISCOVERS the capabilities that
did not load (arcagent owns loading); ``arctrust.approve`` / ``arctrust.disapprove``
MUTATE the approval store (arctrust owns trust). These tests exercise the two
together the way the ``arc trust`` CLI and arcui ``/api/trust`` routes do: list to
find the gated item + its pin, persist a pin via arctrust, then re-scan through
the inventory to confirm the load verdict flipped. The pure persistence and the
TOFU decision are tested in arctrust; here we pin the integration.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust import approve as _approve_pin
from arctrust import disapprove as _disapprove_pin
from arctrust import hash_source, load_validators
from arctrust.identity import AgentIdentity

from arcagent.capabilities import artifact_signing
from arcagent.capabilities.inventory import (
    GatedItem,
    list_gated,
    pin_name_for,
    read_capability_source,
)

_VALID_SKILL = (
    "---\n"
    "name: {name}\n"
    "version: 2.0.0\n"
    "description: does {name}\n"
    "triggers: [{name}]\n"
    "tools: [reload]\n"
    "---\n"
    "\n## Resources\n\n## Contract\n\n## Knowledge\n\n## Steps\n\n"
    "## Anti Patterns\n\n## Examples\n\n## Validation\n"
)

_NO_GLOBAL = "no-global"


@pytest.fixture(autouse=True)
def _hermetic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point ${ARC_CONFIG_DIR} at an empty dir so the config never inherits the
    # developer's ~/.arc base config.
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "empty-arc"))


def _write_skill(root: Path, name: str, *, sign_with: AgentIdentity | None) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    skill_md = folder / "SKILL.md"
    content = _VALID_SKILL.format(name=name).encode("utf-8")
    skill_md.write_bytes(content)
    if sign_with is not None:
        artifact_signing.write_signature(
            skill_md, content, signer_did=sign_with.did, private_key=sign_with.signing_seed
        )
    return skill_md


def _build_agent(tmp_path: Path, *, tier: str) -> tuple[Path, AgentIdentity]:
    """Persist an on-disk agent (identity + arcagent.toml), return (root, identity)."""
    agent_root = tmp_path / "agent"
    (agent_root / "workspace" / "capabilities" / "skills").mkdir(parents=True)
    key_dir = tmp_path / "keys"
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    identity.save_keys(key_dir)
    (agent_root / "arcagent.toml").write_text(
        "[agent]\n"
        'name = "fixture-agent"\n'
        'org = "arc"\n'
        'type = "exec"\n'
        f'workspace = "{agent_root / "workspace"}"\n'
        "\n[llm]\n"
        'model = "test/model"\n'
        "\n[security]\n"
        f'tier = "{tier}"\n'
        "\n[identity]\n"
        f'did = "{identity.did}"\n'
        f'key_dir = "{key_dir}"\n'
        'vault_path = ""\n',
        encoding="utf-8",
    )
    return agent_root, identity


def _skills_root(agent_root: Path) -> Path:
    return agent_root / "workspace" / "capabilities" / "skills"


async def _approve(agent_root: Path, name: str, tmp_path: Path, **kw: str) -> GatedItem:
    """Mirror the caller orchestration: list → pin via arctrust → re-scan.

    Reproduces exactly what ``arc trust approve`` and ``/api/trust/approve`` do,
    so this test covers the seam split (discovery in arcagent, mutation in arctrust).
    """
    gated = await list_gated(agent_root, agent_id=kw.get("agent_id", ""), global_root=tmp_path / _NO_GLOBAL)
    target = next((item for item in gated if item.name == name), None)
    if target is None:
        raise ValueError(f"no gated capability named {name!r}")
    source = read_capability_source(Path(target.path))
    assert source is not None
    _approve_pin(
        agent_root / "arcagent.toml",
        name=pin_name_for(target),
        source=source,
        approver=kw["approver"],
        timestamp=kw["timestamp"],
    )
    after = await list_gated(
        agent_root,
        agent_id=kw.get("agent_id", ""),
        global_root=tmp_path / _NO_GLOBAL,
        include_loaded=True,
    )
    resolved = next((item for item in after if item.name == name), target)
    return resolved


@pytest.mark.asyncio
async def test_list_gated_returns_only_gated_and_computes_hash(tmp_path: Path) -> None:
    agent_root, identity = _build_agent(tmp_path, tier="personal")
    _write_skill(_skills_root(agent_root), "signed", sign_with=identity)
    unsigned_md = _write_skill(_skills_root(agent_root), "unsigned", sign_with=None)

    gated = await list_gated(
        agent_root, agent_id="fixture-agent", global_root=tmp_path / _NO_GLOBAL
    )

    names = {item.name for item in gated}
    assert "unsigned" in names  # personal + unsigned -> DENY, gated
    assert "signed" not in names  # personal + signed -> loaded, not gated
    unsigned = next(item for item in gated if item.name == "unsigned")
    assert unsigned.kind == "skill"
    assert unsigned.status == "deny"
    assert unsigned.agent_id == "fixture-agent"
    assert unsigned.agent_label == "fixture-agent"
    assert unsigned.path == str(unsigned_md)
    assert unsigned.hash == hash_source(unsigned_md.read_bytes().decode("utf-8"))


@pytest.mark.asyncio
async def test_include_loaded_returns_the_loaded_items_too(tmp_path: Path) -> None:
    agent_root, identity = _build_agent(tmp_path, tier="personal")
    _write_skill(_skills_root(agent_root), "signed", sign_with=identity)

    everything = await list_gated(
        agent_root, global_root=tmp_path / _NO_GLOBAL, include_loaded=True
    )
    loaded = next(item for item in everything if item.name == "signed")
    assert loaded.status == "loaded"


@pytest.mark.asyncio
async def test_approve_pins_hash_persists_and_flips_to_loaded_enterprise(tmp_path: Path) -> None:
    agent_root, identity = _build_agent(tmp_path, tier="enterprise")
    # Signed so it passes the enterprise signature floor; first-sight -> new_sighting.
    skill_md = _write_skill(_skills_root(agent_root), "reporter", sign_with=identity)

    before = await list_gated(agent_root, global_root=tmp_path / _NO_GLOBAL)
    assert next(item for item in before if item.name == "reporter").status == "new_sighting"

    result = await _approve(
        agent_root,
        "reporter",
        tmp_path,
        approver="did:arc:ui:operator",
        timestamp="2026-07-14T00:00:00Z",
        agent_id="fixture-agent",
    )
    # Re-scan shows the capability now loads.
    assert result.status == "loaded"

    # The pin persisted to arcagent.toml under the loader's key (the folder name).
    approved = load_validators(agent_root / "arcagent.toml").approved
    assert len(approved) == 1
    assert approved[0].name == "reporter"
    assert approved[0].hash == hash_source(skill_md.read_bytes().decode("utf-8"))
    assert approved[0].approver == "did:arc:ui:operator"
    assert approved[0].timestamp == "2026-07-14T00:00:00Z"

    # A fresh scan no longer lists it as gated.
    after = await list_gated(agent_root, global_root=tmp_path / _NO_GLOBAL)
    assert "reporter" not in {item.name for item in after}


@pytest.mark.asyncio
async def test_approve_at_personal_persists_pin_but_does_not_load(tmp_path: Path) -> None:
    agent_root, _ = _build_agent(tmp_path, tier="personal")
    _write_skill(_skills_root(agent_root), "unsigned", sign_with=None)

    result = await _approve(
        agent_root, "unsigned", tmp_path, approver="op", timestamp="2026-07-14T00:00:00Z"
    )
    # Personal never consults pins: the pin is recorded, but the load verdict is
    # unchanged. The re-scan reports the truth rather than a fake success.
    assert result.status == "deny"
    approved = load_validators(agent_root / "arcagent.toml").approved
    assert [entry.name for entry in approved] == ["unsigned"]


@pytest.mark.asyncio
async def test_disapprove_removes_the_pin_and_regates_enterprise(tmp_path: Path) -> None:
    agent_root, identity = _build_agent(tmp_path, tier="enterprise")
    _write_skill(_skills_root(agent_root), "reporter", sign_with=identity)
    await _approve(
        agent_root, "reporter", tmp_path, approver="op", timestamp="2026-07-14T00:00:00Z"
    )

    removed = _disapprove_pin(agent_root / "arcagent.toml", name="reporter")
    assert removed is True
    assert load_validators(agent_root / "arcagent.toml").approved == ()

    # With the pin gone the capability is gated again (first-sight).
    after = await list_gated(agent_root, global_root=tmp_path / _NO_GLOBAL)
    assert next(item for item in after if item.name == "reporter").status == "new_sighting"


@pytest.mark.asyncio
async def test_disapprove_unknown_name_is_false(tmp_path: Path) -> None:
    agent_root, _ = _build_agent(tmp_path, tier="personal")
    assert _disapprove_pin(agent_root / "arcagent.toml", name="ghost") is False


@pytest.mark.asyncio
async def test_approve_unknown_name_raises(tmp_path: Path) -> None:
    agent_root, _ = _build_agent(tmp_path, tier="personal")
    with pytest.raises(ValueError, match="no gated capability"):
        await _approve(agent_root, "ghost", tmp_path, approver="op", timestamp="t")
