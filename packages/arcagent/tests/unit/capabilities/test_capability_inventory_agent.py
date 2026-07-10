"""SPEC arcui-reality-mirror COMP-007 companion — agent-aware inventory.

`collect_agent_capability_inventory` resolves an agent's real trust posture
(tier -> require_signature, pinned DID key, import policy) exactly as
``agent_lifecycle.setup_capabilities`` does — through the SAME
``resolve_trust_posture`` helper — then runs the frozen inventory seam. This is
what lets arcui mirror reality faithfully across tiers instead of re-deriving
security posture on its own.

Covered here:
  * ``resolve_trust_posture`` maps tier to the loader's trust arguments.
  * the companion reads an agent dir + its on-disk identity and reports
    signed/unsigned/invalid verdicts verbatim (personal AND federal posture).
  * when handed a live agent it also returns that agent's runtime-registered
    tool list (REQ-095), flagged ``runtime=True``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust.identity import AgentIdentity

from arcagent.capabilities import artifact_signing
from arcagent.core.config import CapabilitiesConfig, SecurityConfig, ValidatorsConfig

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

_INVALID_SKILL = (
    "---\nname: {name}\nversion: 1.0.0\ndescription: broken\n"
    "triggers: [{name}]\ntools: [reload]\n---\n\nno sections\n"
)


def _write_skill(root: Path, name: str, *, sign_with: AgentIdentity | None) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    skill_md = folder / "SKILL.md"
    content = _VALID_SKILL.format(name=name).encode("utf-8")
    skill_md.write_bytes(content)
    if sign_with is not None:
        artifact_signing.write_signature(
            skill_md, content, signer_did=sign_with.did, private_key=sign_with.signing_seed
        )


def _write_invalid_skill(root: Path, name: str) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(_INVALID_SKILL.format(name=name), encoding="utf-8")


def _build_agent_dir(tmp_path: Path, *, tier: str) -> tuple[Path, AgentIdentity]:
    """Persist an agent identity + arcagent.toml, return (config_path, identity).

    Mirrors a real on-disk agent so the companion resolves the pinned key from
    the config's DID + key_dir (the non-loaded path).
    """
    agent_root = tmp_path / "agent"
    # Skills live in the capabilities/skills/ subdir (where create_skill writes);
    # the loader scans it via the workspace-skills root.
    (agent_root / "workspace" / "capabilities" / "skills").mkdir(parents=True)
    key_dir = tmp_path / "keys"
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    identity.save_keys(key_dir)
    config_path = agent_root / "arcagent.toml"
    config_path.write_text(
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
    return config_path, identity


@pytest.fixture(autouse=True)
def _hermetic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # load_config merges ${ARC_CONFIG_DIR}/arcagent.toml — point it at an empty
    # dir so the test never inherits the developer's ~/.arc base config.
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "empty-arc"))


class TestResolveTrustPosture:
    def test_personal_tier_does_not_require_signature(self) -> None:
        from arcagent.capabilities.inventory import resolve_trust_posture

        posture = resolve_trust_posture(
            SecurityConfig(tier="personal", validators=ValidatorsConfig()),
            CapabilitiesConfig(),
            trusted_public_key=b"k",
        )
        assert posture.require_signature is False
        assert posture.trusted_public_key == b"k"

    def test_federal_tier_requires_signature(self) -> None:
        from arcagent.capabilities.inventory import resolve_trust_posture

        posture = resolve_trust_posture(
            SecurityConfig(tier="federal", validators=ValidatorsConfig()),
            CapabilitiesConfig(),
            trusted_public_key=b"k",
        )
        assert posture.require_signature is True


@pytest.mark.asyncio
class TestCollectAgentInventory:
    async def test_personal_agent_dir_verbatim_verdicts(self, tmp_path: Path) -> None:
        from arcagent.capabilities.inventory import collect_agent_capability_inventory
        from arcagent.core.tofu_layer import Decision

        config_path, identity = _build_agent_dir(tmp_path, tier="personal")
        ws_skills = config_path.parent / "workspace" / "capabilities" / "skills"
        _write_skill(ws_skills, "signed", sign_with=identity)
        _write_skill(ws_skills, "unsigned", sign_with=None)
        _write_invalid_skill(ws_skills, "broken")

        result = await collect_agent_capability_inventory(
            config_path, global_root=tmp_path / "no-global"
        )
        by_name = {it.name: it for it in result.items}
        assert by_name["signed"].status == "loaded"
        assert by_name["unsigned"].status == Decision.DENY.value
        assert by_name["broken"].status == "invalid"
        assert result.runtime is False
        assert result.runtime_tools == []

    async def test_federal_posture_denies_unsigned_as_truth(self, tmp_path: Path) -> None:
        from arcagent.capabilities.inventory import collect_agent_capability_inventory

        config_path, identity = _build_agent_dir(tmp_path, tier="federal")
        ws_skills = config_path.parent / "workspace" / "capabilities" / "skills"
        _write_skill(ws_skills, "unsigned", sign_with=None)

        result = await collect_agent_capability_inventory(
            config_path, global_root=tmp_path / "no-global"
        )
        item = {it.name: it for it in result.items}["unsigned"]
        # Federal signature floor: unsigned never loads. Faithful, not a bug.
        assert item.status != "loaded"

    async def test_live_agent_returns_runtime_tools(self, tmp_path: Path) -> None:
        from arcagent.capabilities.inventory import collect_agent_capability_inventory
        from arcagent.core.tool_registry import RegisteredTool, ToolTransport

        config_path, identity = _build_agent_dir(tmp_path, tier="personal")

        class _FakeLiveAgent:
            def __init__(self, ident: AgentIdentity) -> None:
                self._identity = ident

            @property
            def registered_tools(self) -> list[RegisteredTool]:
                return [
                    RegisteredTool(
                        name="bash",
                        description="run shell",
                        input_schema={},
                        transport=ToolTransport.NATIVE,
                        execute=None,
                        classification="external_effect",
                    )
                ]

        result = await collect_agent_capability_inventory(
            config_path,
            live_agent=_FakeLiveAgent(identity),
            global_root=tmp_path / "no-global",
        )
        assert result.runtime is True
        names = {t.name for t in result.runtime_tools}
        assert "bash" in names
        tool = next(t for t in result.runtime_tools if t.name == "bash")
        assert tool.classification == "external_effect"


class TestRegisteredToolsAccessor:
    def test_registered_tools_lists_registry_contents(self) -> None:
        from arcagent.core.agent import ArcAgent
        from arcagent.core.config import AgentConfig, ArcAgentConfig, IdentityConfig, LLMConfig
        from arcagent.core.tool_registry import RegisteredTool, ToolTransport

        agent = ArcAgent(
            config=ArcAgentConfig(
                agent=AgentConfig(name="a", org="arc", type="exec"),
                llm=LLMConfig(model="test/model"),
                identity=IdentityConfig(did="", key_dir="/tmp/k", vault_path=""),
            )
        )
        assert agent.registered_tools == []  # before startup

        tool = RegisteredTool(
            name="read",
            description="read a file",
            input_schema={},
            transport=ToolTransport.NATIVE,
            execute=None,
            classification="read_only",
        )

        class _StubRegistry:
            def __init__(self) -> None:
                self.tools = {"read": tool}

        agent._tool_registry = _StubRegistry()  # type: ignore[assignment]  # reason: read-only .tools stub; accessor only reads it
        names = {t.name for t in agent.registered_tools}
        assert names == {"read"}
