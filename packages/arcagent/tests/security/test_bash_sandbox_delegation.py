"""SPEC-035 REQ-020/021/022/023/024 — bash confinement at ent/fed.

The builtin ``bash`` must delegate to arcrun's tier-routed isolation backend at
enterprise/federal (never host ``create_subprocess_shell``), mounting the
workspace read-write, protected files read-only, and never mounting host
``~/.arc``. Personal keeps host bash. The live tests run only when Docker is
available (like SPEC-036).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from arcagent.builtins.capabilities import _runtime

_DOCKER = shutil.which("docker") is not None


@pytest.fixture(autouse=True)
def _reset() -> None:
    _runtime.reset()


@pytest.mark.asyncio
class TestTierDelegation:
    async def test_enterprise_delegates_to_arcrun_run_shell(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import arcrun

        from arcagent.builtins.capabilities.bash import bash

        (tmp_path / "identity.md").write_text("goal\n")
        captured: dict[str, Any] = {}

        async def fake_run_shell(command: str, **kwargs: Any) -> str:
            captured["command"] = command
            captured.update(kwargs)
            return json.dumps({"stdout": "sandboxed", "stderr": "", "exit_code": 0})

        monkeypatch.setattr(arcrun, "run_shell", fake_run_shell)
        _runtime.configure(
            workspace=tmp_path,
            protected_paths=_runtime_protected(tmp_path),
            tier="enterprise",
        )
        result = await bash(command="echo hi")
        assert "sandboxed" in result
        # Delegated with tier + workspace + read-only protected subpath.
        assert captured["tier"] == "enterprise"
        assert Path(captured["workspace"]) == tmp_path.resolve()
        assert Path("identity.md") in list(captured["readonly_subpaths"])

    async def test_personal_uses_host_bash(self, tmp_path: Path) -> None:
        from arcagent.builtins.capabilities.bash import bash

        _runtime.configure(
            workspace=tmp_path,
            protected_paths=_runtime_protected(tmp_path),
            tier="personal",
        )
        result = await bash(command="echo hostbash")
        assert "hostbash" in result


@pytest.mark.skipif(not _DOCKER, reason="requires a Docker daemon")
@pytest.mark.asyncio
class TestSandboxLive:
    async def test_workspace_write_and_protected_readonly(self, tmp_path: Path) -> None:
        from arcagent.builtins.capabilities.bash import bash

        (tmp_path / "identity.md").write_text("operator goal\n")
        _runtime.configure(
            workspace=tmp_path,
            protected_paths=_runtime_protected(tmp_path),
            tier="enterprise",
        )
        # A normal workspace write lands on the host workspace.
        await bash(command="echo hi > note.txt")
        assert (tmp_path / "note.txt").read_text().strip() == "hi"
        # REQ-023: rewriting the read-only-mounted protected file fails.
        result = await bash(command="echo hijack > identity.md")
        assert (tmp_path / "identity.md").read_text() == "operator goal\n"
        assert "Exit code" in result or "denied" in result.lower() or "read-only" in result.lower()

    async def test_operator_seed_unreachable(self, tmp_path: Path) -> None:
        # REQ-021: the host ~/.arc/operator seed is never mounted → absent.
        from arcagent.builtins.capabilities.bash import bash

        _runtime.configure(
            workspace=tmp_path,
            protected_paths=_runtime_protected(tmp_path),
            tier="enterprise",
        )
        result = await bash(command="cat ~/.arc/operator/operator.key")
        assert "Exit code" in result  # non-zero: no such file inside the sandbox


def _runtime_protected(ws: Path) -> frozenset[Path]:
    from arcagent.tools._validation import resolve_protected_paths

    return resolve_protected_paths(ws, [])
