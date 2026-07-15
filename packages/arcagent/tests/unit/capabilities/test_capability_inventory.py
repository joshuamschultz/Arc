"""SPEC arcui-reality-mirror COMP-007 (T-704) — capability inventory seam.

The seam drives :class:`CapabilityLoader` across the four scan roots
(builtins, global ``~/.arc/capabilities``, per-agent, agent-authored
workspace) and returns one typed record per discovered skill / capability
tool, carrying the loader/TOFU verdict VERBATIM.

These tests pin the contract that matters for REQ-093/094:

  * every scan root is enumerated (source_root is the loader's root name);
  * a signed workspace skill loads, an unsigned one is TOFU-denied, and a
    malformed one is invalid;
  * the ``status`` string is exactly what the loader recorded — for the
    denial it must equal :data:`TofuDecision.DENY` verbatim, proving the seam
    invents no status literals of its own.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust import TofuDecision, TofuLayer, ValidatorsConfig
from arctrust.identity import AgentIdentity

from arcagent.capabilities import artifact_signing
from arcagent.capabilities.inventory import CapabilityInventoryItem, collect_capability_inventory
from arcagent.core.tier import Tier

_VALID_SKILL = (
    "---\n"
    "name: {name}\n"
    "version: 1.2.3\n"
    "description: does {name}\n"
    "triggers: [{name}]\n"
    "tools: [reload]\n"
    "---\n"
    "\n## Resources\n\n## Contract\n\n## Knowledge\n\n## Steps\n\n"
    "## Anti Patterns\n\n## Examples\n\n## Validation\n"
)

# Frontmatter present, required sections absent -> skill_validator errors,
# entry is None -> loader records status "invalid".
_INVALID_SKILL = (
    "---\n"
    "name: {name}\n"
    "version: 1.0.0\n"
    "description: broken {name}\n"
    "triggers: [{name}]\n"
    "tools: [reload]\n"
    "---\n"
    "\nno required sections here\n"
)

_TOOL = (
    "from arcagent.tools._decorator import tool\n"
    "@tool(description='echoes', version='4.5.6')\n"
    "async def {name}() -> str:\n"
    "    return 'ok'\n"
)


def _identity() -> AgentIdentity:
    return AgentIdentity.generate(org="arc", agent_type="exec")


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


def _write_tool(root: Path, name: str) -> None:
    (root / f"{name}.py").write_text(_TOOL.format(name=name), encoding="utf-8")


@pytest.fixture
def agent_tree(tmp_path: Path) -> dict[str, Path]:
    """Materialise a builtins root, a global root, and an agent dir.

    Layout mirrors :func:`arcagent.core.agent_lifecycle.setup_capabilities`:
    ``<agent>/capabilities`` and ``<agent>/workspace/capabilities`` plus the
    package builtins and the global ``~/.arc/capabilities`` root (both
    injected as tmp dirs so the test never touches real install state).
    """
    builtins = tmp_path / "builtins"
    (builtins / "skills").mkdir(parents=True)
    global_root = tmp_path / "global"
    global_root.mkdir()
    agent_dir = tmp_path / "agent"
    (agent_dir / "capabilities").mkdir(parents=True)
    (agent_dir / "workspace" / "capabilities").mkdir(parents=True)
    return {
        "builtins": builtins,
        "global": global_root,
        "agent_dir": agent_dir,
        "agent_caps": agent_dir / "capabilities",
        "workspace_caps": agent_dir / "workspace" / "capabilities",
    }


def _by_name(
    items: list[CapabilityInventoryItem],
) -> dict[str, CapabilityInventoryItem]:
    return {it.name: it for it in items}


@pytest.mark.asyncio
class TestCollectCapabilityInventoryDoesNotSpawnBackgroundTasks:
    """Task #39: this is "the arcui inventory seam's throwaway registry" —
    one of the three read-only CapabilityLoader construction sites that must
    never actually START a @background_task it discovers while scanning
    (e.g. the memory module's consolidation loop, whose body depends on a
    live agent's module _runtime being configured).
    """

    async def test_loader_constructed_with_spawn_background_tasks_false(
        self, agent_tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import arcagent.capabilities.inventory as inventory_mod
        from arcagent.capabilities.capability_loader import CapabilityLoader

        captured: dict[str, object] = {}
        real_init = CapabilityLoader.__init__

        def _spy_init(self: object, **kwargs: object) -> None:
            captured.update(kwargs)
            real_init(self, **kwargs)  # type: ignore[arg-type]

        class _SpyLoader(CapabilityLoader):
            __init__ = _spy_init

        monkeypatch.setattr(inventory_mod, "CapabilityLoader", _SpyLoader)

        await collect_capability_inventory(
            agent_tree["agent_dir"],
            builtins_root=agent_tree["builtins"],
            global_root=agent_tree["global"],
        )

        assert captured.get("spawn_background_tasks") is False


@pytest.mark.asyncio
class TestCapabilityInventory:
    async def _collect(
        self, agent_tree: dict[str, Path], identity: AgentIdentity
    ) -> list[CapabilityInventoryItem]:
        tofu = TofuLayer(Tier.PERSONAL, ValidatorsConfig())
        return await collect_capability_inventory(
            agent_tree["agent_dir"],
            builtins_root=agent_tree["builtins"],
            global_root=agent_tree["global"],
            tofu=tofu,
            trusted_public_key=identity.public_key,
        )

    async def test_enumerates_all_four_scan_roots(self, agent_tree: dict[str, Path]) -> None:
        identity = _identity()
        # builtins skill (trusted root -> loads), a builtin tool,
        # a signed global skill, a signed agent skill, a signed workspace skill.
        _write_skill(agent_tree["builtins"] / "skills", "core-skill", sign_with=None)
        _write_tool(agent_tree["builtins"], "echo")
        _write_skill(agent_tree["global"], "global-skill", sign_with=identity)
        _write_skill(agent_tree["agent_caps"], "agent-skill", sign_with=identity)
        _write_skill(agent_tree["workspace_caps"], "ws-signed", sign_with=identity)

        items = await self._collect(agent_tree, identity)
        roots = {it.source_root for it in items}
        assert {"builtins-skills", "builtins", "global", "agent", "workspace"} <= roots

    async def test_workspace_skills_subdir_is_scanned(self, agent_tree: dict[str, Path]) -> None:
        # create_skill / update_skill write to <root>/capabilities/skills/<name>/,
        # the same shape as the builtins-skills root — the loader must scan it.
        identity = _identity()
        subdir = agent_tree["workspace_caps"] / "skills"
        _write_skill(subdir, "authored", sign_with=identity)

        items = await self._collect(agent_tree, identity)
        item = _by_name(items)["authored"]
        assert item.kind == "skill"
        assert item.source_root == "workspace-skills"
        assert item.status == "loaded"

    async def test_workspace_skills_subdir_is_trust_gated(
        self, agent_tree: dict[str, Path]
    ) -> None:
        # The skills subdir is agent-writable, so it must pass the same TOFU
        # gate as the capabilities root — an unsigned authored skill is denied.
        identity = _identity()
        subdir = agent_tree["workspace_caps"] / "skills"
        _write_skill(subdir, "authored-unsigned", sign_with=None)

        items = await self._collect(agent_tree, identity)
        item = _by_name(items)["authored-unsigned"]
        assert item.source_root == "workspace-skills"
        assert item.status == TofuDecision.DENY.value

    async def test_signed_workspace_skill_loads_with_metadata(
        self, agent_tree: dict[str, Path]
    ) -> None:
        identity = _identity()
        _write_skill(agent_tree["workspace_caps"], "ws-signed", sign_with=identity)

        items = await self._collect(agent_tree, identity)
        item = _by_name(items)["ws-signed"]
        assert item.kind == "skill"
        assert item.source_root == "workspace"
        assert item.status == "loaded"
        assert item.version == "1.2.3"
        assert item.description == "does ws-signed"

    async def test_unsigned_workspace_skill_is_tofu_denied_verbatim(
        self, agent_tree: dict[str, Path]
    ) -> None:
        identity = _identity()
        _write_skill(agent_tree["workspace_caps"], "ws-unsigned", sign_with=None)

        items = await self._collect(agent_tree, identity)
        item = _by_name(items)["ws-unsigned"]
        assert item.source_root == "workspace"
        # The status is the TofuLayer verdict, not a seam-side literal.
        assert item.status == TofuDecision.DENY.value

    async def test_invalid_workspace_skill_reported_invalid(
        self, agent_tree: dict[str, Path]
    ) -> None:
        identity = _identity()
        _write_invalid_skill(agent_tree["workspace_caps"], "ws-broken")

        items = await self._collect(agent_tree, identity)
        item = _by_name(items)["ws-broken"]
        assert item.kind == "skill"
        assert item.status == "invalid"
        assert item.status_detail  # carries the validator's reason

    async def test_capability_tool_enumerated_as_loaded(self, agent_tree: dict[str, Path]) -> None:
        identity = _identity()
        _write_tool(agent_tree["builtins"], "echo")

        items = await self._collect(agent_tree, identity)
        item = _by_name(items)["echo"]
        assert item.kind == "tool"
        assert item.source_root == "builtins"
        assert item.status == "loaded"
        assert item.version == "4.5.6"

    async def test_only_skill_and_tool_kinds_surface(self, agent_tree: dict[str, Path]) -> None:
        identity = _identity()
        _write_skill(agent_tree["workspace_caps"], "ws-signed", sign_with=identity)
        _write_tool(agent_tree["builtins"], "echo")

        items = await self._collect(agent_tree, identity)
        assert items  # non-empty
        assert all(it.kind in {"skill", "tool"} for it in items)
