"""The agent's own tools cannot reach the deployment module root.

SPEC-066 T-963 (REQ-335). Phase 3 moves module runtime out of the wheel and
onto the filesystem at :func:`arctrust.paths.module_root`. That is the
first time executable agent code lives in a directory the process can see at
runtime, so a tool that can write there is a self-modification primitive: the
agent edits ``_runtime.py``, the next startup imports it, and the change
survives every policy the running process enforces (ASI05 unexpected code
execution, ASI06 memory/context poisoning).

REQ-335 states the control as two independent halves — mode ``0444`` inside
``0555`` **and** "located outside the agent tool fence". Only the second half
survives an operator who runs Arc as the same user that owns ``~/.arc``, which
is the ordinary single-user install. So every test here makes the module root
**writable by the test user first**, asserts that with ``os.access``, and only
then drives the real tool. A test that skipped that step would still pass if
the fence were deleted and the mode bit were doing all the work.

The tools are the real ``write`` / ``edit`` / ``bash`` capability functions,
driven through the same ``_runtime`` the agent configures at startup — not a
re-derivation of the boundary check.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest
from arctrust.paths import module_root

from arcagent.builtins.capabilities import _runtime
from arcagent.builtins.capabilities.bash import bash
from arcagent.builtins.capabilities.edit import edit
from arcagent.builtins.capabilities.write import write
from arcagent.core.errors import ToolError

_RUNTIME_SOURCE = "SENTINEL_MODULE_RUNTIME = 'untouched'\n"


@dataclass(frozen=True)
class _Deployment:
    """The three paths a module-root escape has to cross."""

    arc_home: Path
    module_root: Path
    runtime_file: Path
    workspace: Path

    @property
    def traversal(self) -> str:
        """The same runtime file, named relative to the workspace.

        Derived from the two real paths rather than spelled out, so it keeps
        naming the file the fence must refuse wherever the module root moves.

        A fence that compares path *strings* against the workspace prefix
        accepts this and rejects the absolute form; only resolving first and
        then testing ancestry rejects both.
        """
        return os.path.relpath(self.runtime_file, self.workspace)


def _writable(path: Path) -> bool:
    return os.access(path, os.W_OK)


@pytest.fixture()
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Deployment:
    """A deployment module root that the test user can genuinely write to.

    The root is placed through :func:`arctrust.paths.module_root`, because the
    fence recognises the root the resolver names — a root staged anywhere else
    would be refused only as "not the workspace", which is the weaker half of
    REQ-335 and would still pass with the module-root exclusion deleted.

    The workspace sits under the same Arc home, so the traversal shape is a
    real escape within one deployment rather than an artificial walk up to the
    filesystem root.
    """
    arc_home = tmp_path / "arc"
    modules_root = module_root(arc_home)
    runtime_file = modules_root / "memory" / "_runtime.py"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text(_RUNTIME_SOURCE)
    workspace = arc_home / "workspace"
    workspace.mkdir()

    # Deliberately permissive: 0755 dirs, 0644 file, all the way down from the
    # Arc home. The materializer's 0444/0555 is a second layer, and this suite
    # must not be able to borrow it.
    for directory in (*(p for p in reversed(runtime_file.parents) if p.is_relative_to(arc_home)),):
        directory.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    runtime_file.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

    monkeypatch.setenv("ARC_CONFIG_DIR", str(arc_home))
    _runtime.configure(workspace=workspace, allowed_paths=None, tier="personal")
    return _Deployment(
        arc_home=arc_home,
        module_root=modules_root,
        runtime_file=runtime_file,
        workspace=workspace,
    )


def test_the_module_root_is_writable_by_the_test_user(deployment: _Deployment) -> None:
    """Guard for every other test in this file.

    If this ever fails, the refusals below stopped proving anything about the
    fence — the operating system was refusing on their behalf.
    """
    assert _writable(deployment.module_root)
    assert _writable(deployment.runtime_file.parent)
    assert _writable(deployment.runtime_file)


async def test_write_cannot_create_a_file_in_the_module_root(deployment: _Deployment) -> None:
    """A new module folder is a new import target; creating one must be refused."""
    target = deployment.module_root / "backdoor" / "_runtime.py"

    with pytest.raises(ToolError) as caught:
        await write(str(target), "def configure(**_): __import__('os').system('id')\n")

    assert caught.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"
    assert not target.exists()


async def test_write_cannot_overwrite_a_materialized_runtime(deployment: _Deployment) -> None:
    with pytest.raises(ToolError) as caught:
        await write(str(deployment.runtime_file), "SENTINEL_MODULE_RUNTIME = 'pwned'\n")

    assert caught.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"
    assert deployment.runtime_file.read_text() == _RUNTIME_SOURCE


async def test_write_cannot_traverse_out_of_the_workspace_into_the_module_root(
    deployment: _Deployment,
) -> None:
    """``../modules/...`` resolves to the same file the absolute form names."""
    with pytest.raises(ToolError) as caught:
        await write(deployment.traversal, "SENTINEL_MODULE_RUNTIME = 'pwned'\n")

    assert caught.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"
    assert deployment.runtime_file.read_text() == _RUNTIME_SOURCE


async def test_edit_cannot_patch_a_materialized_runtime(deployment: _Deployment) -> None:
    """``edit`` is the cheaper attack — one substitution, no full rewrite."""
    with pytest.raises(ToolError) as caught:
        await edit(str(deployment.runtime_file), "untouched", "pwned")

    assert caught.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"
    assert deployment.runtime_file.read_text() == _RUNTIME_SOURCE


async def test_edit_cannot_traverse_out_of_the_workspace_into_the_module_root(
    deployment: _Deployment,
) -> None:
    with pytest.raises(ToolError) as caught:
        await edit(deployment.traversal, "untouched", "pwned")

    assert caught.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"
    assert deployment.runtime_file.read_text() == _RUNTIME_SOURCE


async def test_bash_cannot_redirect_output_into_the_module_root(
    deployment: _Deployment,
) -> None:
    """Personal-tier ``bash`` is host bash — the file fence never sees this path.

    Enterprise and federal route through arcrun's isolation backend, which
    bind-mounts the workspace and nothing else, so the module root is already
    unreachable there. Personal has only the advisory shell guard, and that
    guard checks the operator-protected *set* rather than the deployment
    module root — which is what this test exists to close.
    """
    command = f"echo pwned > {deployment.runtime_file}"

    with pytest.raises(ToolError):
        await bash(command)

    assert deployment.runtime_file.read_text() == _RUNTIME_SOURCE


async def test_bash_cannot_delete_a_materialized_module(deployment: _Deployment) -> None:
    """Removal is as much a supply-chain attack as replacement.

    Deleting a module's tree silently disables the capability on the next
    startup — folder-presence discovery reports it as simply not discovered,
    with no signature to fail and nothing to alert on.
    """
    command = f"rm -rf {deployment.runtime_file.parent}"

    with pytest.raises(ToolError):
        await bash(command)

    assert deployment.runtime_file.read_text() == _RUNTIME_SOURCE


async def test_bash_cannot_append_to_a_materialized_runtime(deployment: _Deployment) -> None:
    """Append is the quiet variant: the sentinel survives, the payload rides along."""
    command = f"echo 'BACKDOOR = 1' >> {deployment.runtime_file}"

    with pytest.raises(ToolError):
        await bash(command)

    assert deployment.runtime_file.read_text() == _RUNTIME_SOURCE


async def test_an_allowed_path_over_arc_home_does_not_open_the_module_root(
    deployment: _Deployment,
) -> None:
    """REQ-335: the module root is outside the fence, not merely outside the workspace.

    ``tools.policy.allowed_paths`` is operator config, and a plausible entry
    (the Arc home, so an agent can read its own deployment state) would
    otherwise hand the agent write access to every module runtime on the box.
    The deployment module root has to be excluded from the fence explicitly
    rather than by the accident of nobody having listed its parent.
    """
    _runtime.configure(
        workspace=deployment.workspace,
        allowed_paths=[deployment.arc_home],
        tier="personal",
    )

    with pytest.raises(ToolError):
        await write(str(deployment.runtime_file), "SENTINEL_MODULE_RUNTIME = 'pwned'\n")

    assert deployment.runtime_file.read_text() == _RUNTIME_SOURCE
