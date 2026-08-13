"""Per-agent capability copy — what travels, what stays, and the exact inverse.

The copy exists to put a module's *callable surface* where the loader already
looks while leaving its *runtime* at the deployment root. These tests hold both
halves of that: what lands in the agent's capability root, and — more
importantly — what must never land there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcbundle import (
    CAPABILITY_FILE,
    SIGNATURE_SIDECAR_SUFFIX,
    SKILLS_DIR,
    BundleError,
    BundleMaterializeError,
    capability_dir,
    copy_capabilities,
    remove_capabilities,
)

_TOOLS = b"from arcagent import tool\n\n\n@tool\ndef fetch(url: str) -> str:\n    return url\n"
_RUNTIME = b"def configure(agent):\n    return None\n"
_SKILL = b"---\nname: browse\n---\n\nHow to browse.\n"


@pytest.fixture
def module_dir(tmp_path: Path) -> Path:
    """A materialized module tree: runtime, capability surface, a skill, and noise."""
    root = tmp_path / "modules" / "browser"
    (root / SKILLS_DIR / "browse").mkdir(parents=True)
    (root / CAPABILITY_FILE).write_bytes(_TOOLS)
    (root / "_runtime.py").write_bytes(_RUNTIME)
    (root / "config.py").write_bytes(b"TIMEOUT = 30\n")
    (root / "backends").mkdir()
    (root / "backends" / "cdp.py").write_bytes(b"# private runtime support\n")
    (root / SKILLS_DIR / "browse" / "SKILL.md").write_bytes(_SKILL)
    return root


@pytest.fixture
def agent_dir(tmp_path: Path) -> Path:
    """An agent config root, as `arc agent create` leaves it."""
    root = tmp_path / "team" / "josh_agent"
    root.mkdir(parents=True)
    (root / "arcagent.toml").write_text("[agent]\nname = 'josh'\n", encoding="utf-8")
    return root


def test_copy_lands_in_the_agent_capability_root(module_dir: Path, agent_dir: Path) -> None:
    """Tools go directly under the root and skills under `skills/` — the layout
    `append_capability_scan_roots` defines. Anything else is never discovered."""
    dest = copy_capabilities(module_dir, agent_dir, module="browser")

    assert dest == capability_dir(agent_dir, "browser")
    assert (dest / CAPABILITY_FILE).read_bytes() == _TOOLS
    assert (dest / SKILLS_DIR / "browse" / "SKILL.md").read_bytes() == _SKILL


def test_copy_leaves_runtime_and_its_support_behind(module_dir: Path, agent_dir: Path) -> None:
    """Runtime in an agent-writable directory would be a self-modification path
    (ASI05/ASI06). The allowlist is two names, so a file added to a module later
    cannot quietly become agent-reachable code either."""
    dest = copy_capabilities(module_dir, agent_dir, module="browser")

    assert not (dest / "_runtime.py").exists()
    assert not (dest / "config.py").exists()
    assert not (dest / "backends").exists()
    # The originals are untouched at the deployment root.
    assert (module_dir / "_runtime.py").read_bytes() == _RUNTIME


def test_copy_carries_the_signature_sidecar(module_dir: Path, agent_dir: Path) -> None:
    """A signed capability must stay signed across the copy, or every install
    would strand its own tools behind the trust gate it just satisfied."""
    sidecar = module_dir / (CAPABILITY_FILE + SIGNATURE_SIDECAR_SUFFIX)
    sidecar.write_text('{"signer_did": "did:arc:release"}', encoding="utf-8")

    dest = copy_capabilities(module_dir, agent_dir, module="browser")

    copied = dest / (CAPABILITY_FILE + SIGNATURE_SIDECAR_SUFFIX)
    assert copied.read_text(encoding="utf-8") == sidecar.read_text(encoding="utf-8")


def test_copy_does_not_add_a_scan_root(module_dir: Path, agent_dir: Path) -> None:
    """Everything written stays inside the one `capabilities/` directory the
    loader already scans — the copy widens no trust boundary (D-648/D-649)."""
    copy_capabilities(module_dir, agent_dir, module="browser")

    written = {path for path in agent_dir.rglob("*") if path.name != "arcagent.toml"}
    caps_root = agent_dir / "capabilities"
    assert written
    assert all(path == caps_root or caps_root in path.parents for path in written)


def test_copy_arrives_writable_even_from_a_hardened_source(
    module_dir: Path, agent_dir: Path
) -> None:
    """The materialized source is 0444 inside 0555. Inheriting that would make
    the copy unremovable AND unsignable — an operator approving a capability
    writes a sidecar beside it — so the modes are normalized, not preserved.
    The copy's trust comes from the root it sits in, never from a mode bit."""
    for path in sorted(module_dir.rglob("*"), reverse=True):
        path.chmod(0o444 if path.is_file() else 0o555)
    module_dir.chmod(0o555)

    dest = copy_capabilities(module_dir, agent_dir, module="browser")

    try:
        assert (dest / CAPABILITY_FILE).stat().st_mode & 0o200
        assert (dest / SKILLS_DIR).stat().st_mode & 0o300 == 0o300
        assert remove_capabilities(agent_dir, module="browser") is True
    finally:
        for path in sorted(module_dir.rglob("*"), reverse=True):
            path.chmod(0o700)
        module_dir.chmod(0o700)


def test_copy_replaces_an_earlier_copy_wholesale(module_dir: Path, agent_dir: Path) -> None:
    """A reinstall must not leave a removed capability behind; a stale tool file
    is a tool the agent can still call."""
    copy_capabilities(module_dir, agent_dir, module="browser")
    stale = capability_dir(agent_dir, "browser") / "dropped.py"
    stale.write_bytes(b"# removed upstream\n")

    copy_capabilities(module_dir, agent_dir, module="browser")

    assert not stale.exists()
    assert (capability_dir(agent_dir, "browser") / CAPABILITY_FILE).exists()


def test_copy_of_a_skill_only_module_succeeds(tmp_path: Path, agent_dir: Path) -> None:
    """A module may contribute prompt material and no tools at all."""
    source = tmp_path / "modules" / "playbooks"
    (source / SKILLS_DIR / "triage").mkdir(parents=True)
    (source / SKILLS_DIR / "triage" / "SKILL.md").write_bytes(_SKILL)
    (source / "_runtime.py").write_bytes(_RUNTIME)

    dest = copy_capabilities(source, agent_dir, module="playbooks")

    assert (dest / SKILLS_DIR / "triage" / "SKILL.md").exists()


def test_copy_refuses_a_module_with_no_capability_surface(tmp_path: Path, agent_dir: Path) -> None:
    """Silently copying nothing would report an install that gave the agent
    nothing — the operator must hear about it."""
    source = tmp_path / "modules" / "hollow"
    source.mkdir(parents=True)
    (source / "_runtime.py").write_bytes(_RUNTIME)

    with pytest.raises(BundleMaterializeError, match=r"no capabilities\.py"):
        copy_capabilities(source, agent_dir, module="hollow")

    assert not capability_dir(agent_dir, "hollow").exists()


def test_copy_refuses_a_missing_source(tmp_path: Path, agent_dir: Path) -> None:
    with pytest.raises(BundleMaterializeError, match="not a materialized module"):
        copy_capabilities(tmp_path / "absent", agent_dir, module="browser")


@pytest.mark.parametrize("hostile", ["../../etc", "..", "a/b", "/abs", ".hidden"])
def test_copy_refuses_a_traversing_module_name(
    module_dir: Path, agent_dir: Path, hostile: str
) -> None:
    """The module name becomes a directory under the agent root, so it is a path
    too — a config entry must not be able to aim the write elsewhere."""
    with pytest.raises(BundleError):
        copy_capabilities(module_dir, agent_dir, module=hostile)


def test_a_failed_copy_leaves_no_staging_litter(tmp_path: Path, agent_dir: Path) -> None:
    """A refusal that leaves a half-tree behind would be discovered by the next
    scan as a partial capability set."""
    source = tmp_path / "modules" / "hollow"
    source.mkdir(parents=True)
    (source / "_runtime.py").write_bytes(_RUNTIME)

    with pytest.raises(BundleMaterializeError):
        copy_capabilities(source, agent_dir, module="hollow")

    caps = agent_dir / "capabilities"
    assert list(caps.iterdir()) == []


def test_remove_deletes_the_copy_and_reports_it(module_dir: Path, agent_dir: Path) -> None:
    copy_capabilities(module_dir, agent_dir, module="browser")

    assert remove_capabilities(agent_dir, module="browser") is True
    assert not capability_dir(agent_dir, "browser").exists()


def test_remove_of_an_absent_copy_is_a_completion_not_a_failure(agent_dir: Path) -> None:
    """An agent that never enabled the module is already in the desired state.
    Raising here would make `arc module remove` fail on every other agent."""
    assert remove_capabilities(agent_dir, module="browser") is False


def test_remove_refuses_to_follow_a_symlink(tmp_path: Path, agent_dir: Path) -> None:
    """A planted link would turn removal into a delete of whatever it points at."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_bytes(b"keep\n")
    planted = capability_dir(agent_dir, "browser")
    planted.parent.mkdir(parents=True)
    planted.symlink_to(victim, target_is_directory=True)

    with pytest.raises(BundleMaterializeError, match="symlink"):
        remove_capabilities(agent_dir, module="browser")

    assert (victim / "keep.txt").exists()


def test_capability_dir_is_the_one_path_rule(agent_dir: Path) -> None:
    """Copy and remove must not be able to disagree about the destination."""
    caps = agent_dir / "capabilities"
    assert capability_dir(agent_dir, "browser") == caps / "modules" / "browser"
    with pytest.raises(BundleError):
        capability_dir(agent_dir, "../escape")


def test_a_module_copy_never_lands_on_a_conventional_loader_root(agent_dir: Path) -> None:
    """The ``modules/`` level exists to stop a name collision, so pin it here.

    ``<agent>/capabilities/skills/`` is already the agent's skills root. Before
    the namespace, a module NAMED ``skills`` copied straight onto it, and its
    ``capabilities.py`` landed where only skill folders belong — the loader read
    it as agent-authored source, denied it, and the permanent scan error blocked
    every reload commit.
    """
    caps = agent_dir / "capabilities"
    assert capability_dir(agent_dir, "skills") != caps / "skills"
    assert caps / "skills" not in capability_dir(agent_dir, "skills").parents
