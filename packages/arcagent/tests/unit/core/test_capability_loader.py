"""SPEC-021 Task 1.6 — CapabilityLoader.

The loader walks four scan roots in precedence order, AST-validates
each ``.py`` file, registers decorated callables with the
:class:`CapabilityRegistry`, parses skill folder ``SKILL.md``
frontmatter, and emits ``capability:added/removed/replaced/
registration_failed/setup_failed`` events.

What 1.6 covers:

  * Scan precedence — workspace > agent > global > builtins
  * Diff format (R-005) — single-line nominal, multi-line on errors
  * Topological setup ordering when capability classes declare
    ``depends_on`` (R-061)
  * Reverse-topological teardown
  * Rollback when a ``setup()`` raises mid-startup

AST validator integration, TOFU layer, OS sandbox, audit emission are
covered in their own tasks; the loader composes them but does not
replicate their tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.core.capability_registry import CapabilityRegistry


def _write_tool(path: Path, name: str, *, version: str = "1.0.0") -> None:
    """Materialise a one-tool .py file under ``path``."""
    body = (
        "from arcagent.tools._decorator import tool\n"
        f'@tool(description="t", version="{version}")\n'
        f"async def {name}() -> str:\n"
        '    return "ok"\n'
    )
    path.write_text(body)


def _write_skill(folder: Path, name: str, *, version: str = "1.0.0") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    body = (
        "---\n"
        f"name: {name}\n"
        f"version: {version}\n"
        f"description: a skill that does {name}\n"
        f"triggers: [{name}]\n"
        "tools: [reload]\n"
        "---\n"
        "\n## Resources\n\n## Contract\n\n## Knowledge\n\n## Steps\n\n"
        "## Anti Patterns\n\n## Examples\n\n## Validation\n"
    )
    (folder / "SKILL.md").write_text(body)


@pytest.fixture
def four_roots(tmp_path: Path) -> dict[str, Path]:
    """Build four empty scan-root directories."""
    roots = {
        "builtins": tmp_path / "builtins",
        "global": tmp_path / "global",
        "agent": tmp_path / "agent",
        "workspace": tmp_path / "workspace",
    }
    for p in roots.values():
        p.mkdir()
    return roots


@pytest.mark.asyncio
class TestScanAndRegister:
    async def test_registers_a_tool(self, four_roots: dict[str, Path]) -> None:
        from arcagent.core.capability_loader import CapabilityLoader

        _write_tool(four_roots["builtins"] / "echo.py", "echo")

        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=list(four_roots.items()), registry=reg)
        await loader.scan_and_register()

        entry = await reg.get_tool("echo")
        assert entry is not None
        assert entry.scan_root == "builtins"

    async def test_registers_a_skill(self, four_roots: dict[str, Path]) -> None:
        from arcagent.core.capability_loader import CapabilityLoader

        _write_skill(four_roots["builtins"] / "create-tool", "create-tool")

        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=list(four_roots.items()), registry=reg)
        await loader.scan_and_register()

        skill = await reg.get_skill("create-tool")
        assert skill is not None
        assert skill.version == "1.0.0"


@pytest.mark.asyncio
class TestScanPrecedence:
    async def test_workspace_overrides_builtins(self, four_roots: dict[str, Path]) -> None:
        """When the same name appears in two roots, later-scanned wins."""
        from arcagent.core.capability_loader import CapabilityLoader

        _write_tool(four_roots["builtins"] / "echo.py", "echo", version="1.0.0")
        _write_tool(four_roots["workspace"] / "echo.py", "echo", version="2.0.0")

        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=list(four_roots.items()), registry=reg)
        await loader.scan_and_register()

        entry = await reg.get_tool("echo")
        assert entry is not None
        assert entry.meta.version == "2.0.0"
        assert entry.scan_root == "workspace"


@pytest.mark.asyncio
class TestReloadDiff:
    async def test_diff_added_only(self, four_roots: dict[str, Path]) -> None:
        from arcagent.core.capability_loader import CapabilityLoader

        _write_tool(four_roots["builtins"] / "a.py", "a")
        _write_tool(four_roots["builtins"] / "b.py", "b")

        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=list(four_roots.items()), registry=reg)
        diff = await loader.reload()

        assert "+2 added" in diff
        assert "0 errors" in diff
        # nominal case is single line
        assert diff.count("\n") == 0

    async def test_diff_with_errors_multi_line(self, four_roots: dict[str, Path]) -> None:
        """Untrusted (workspace) source goes through AST validator;
        a privileged import rejects with `1 error` in the diff."""
        from arcagent.core.capability_loader import CapabilityLoader

        _write_tool(four_roots["builtins"] / "ok.py", "ok")
        # Workspace = untrusted root → AST-validated.
        (four_roots["workspace"] / "bad.py").write_text("import os\n")

        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=list(four_roots.items()), registry=reg)
        diff = await loader.reload()

        assert "+1 added" in diff
        assert "1 error" in diff
        assert "\n" in diff  # error path is multi-line

    async def test_replaced_segment(self, four_roots: dict[str, Path]) -> None:
        from arcagent.core.capability_loader import CapabilityLoader

        _write_tool(four_roots["builtins"] / "x.py", "x", version="1.0.0")
        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=list(four_roots.items()), registry=reg)
        await loader.reload()

        # Update version
        _write_tool(four_roots["builtins"] / "x.py", "x", version="2.0.0")
        diff = await loader.reload()
        assert "~1 replaced" in diff
        assert "1.0.0" in diff and "2.0.0" in diff


@pytest.mark.asyncio
class TestCapabilityLifecycleOrder:
    async def test_topo_setup_and_reverse_teardown(self, four_roots: dict[str, Path]) -> None:
        """A → B → C dependency chain. Setup A,B,C; shutdown C,B,A."""
        from arcagent.core.capability_loader import CapabilityLoader

        order: list[str] = []

        # Class capabilities are written via test fixtures rather than
        # source files for isolation — they register through a hook.
        from arcagent.core.capability_registry import LifecycleEntry
        from arcagent.tools._decorator import (
            CapabilityClassMetadata,
        )

        class CapA:
            async def setup(self, ctx: object) -> None:
                order.append("setup:A")

            async def teardown(self) -> None:
                order.append("teardown:A")

        class CapB:
            async def setup(self, ctx: object) -> None:
                order.append("setup:B")

            async def teardown(self) -> None:
                order.append("teardown:B")

        class CapC:
            async def setup(self, ctx: object) -> None:
                order.append("setup:C")

            async def teardown(self) -> None:
                order.append("teardown:C")

        reg = CapabilityRegistry()
        await reg.register_capability(
            LifecycleEntry(
                meta=CapabilityClassMetadata(name="C", depends_on=("B",)),
                instance=CapC(),
                source_path=Path("/c.py"),
                scan_root="builtins",
            )
        )
        await reg.register_capability(
            LifecycleEntry(
                meta=CapabilityClassMetadata(name="A"),
                instance=CapA(),
                source_path=Path("/a.py"),
                scan_root="builtins",
            )
        )
        await reg.register_capability(
            LifecycleEntry(
                meta=CapabilityClassMetadata(name="B", depends_on=("A",)),
                instance=CapB(),
                source_path=Path("/b.py"),
                scan_root="builtins",
            )
        )

        loader = CapabilityLoader(scan_roots=list(four_roots.items()), registry=reg)
        await loader.start_lifecycles()
        assert order == ["setup:A", "setup:B", "setup:C"]

        await loader.shutdown()
        assert order[3:] == ["teardown:C", "teardown:B", "teardown:A"]

    async def test_rollback_on_setup_failure(self, four_roots: dict[str, Path]) -> None:
        """If B's setup raises, A's teardown runs; C never starts."""
        from arcagent.core.capability_loader import CapabilityLoader
        from arcagent.core.capability_registry import LifecycleEntry
        from arcagent.tools._decorator import CapabilityClassMetadata

        order: list[str] = []

        class CapA:
            async def setup(self, ctx: object) -> None:
                order.append("setup:A")

            async def teardown(self) -> None:
                order.append("teardown:A")

        class CapB:
            async def setup(self, ctx: object) -> None:
                order.append("setup:B")
                raise RuntimeError("boom")

            async def teardown(self) -> None:
                order.append("teardown:B")

        class CapC:
            async def setup(self, ctx: object) -> None:
                order.append("setup:C")

            async def teardown(self) -> None:
                order.append("teardown:C")

        reg = CapabilityRegistry()
        await reg.register_capability(
            LifecycleEntry(
                meta=CapabilityClassMetadata(name="A"),
                instance=CapA(),
                source_path=Path("/a.py"),
                scan_root="builtins",
            )
        )
        await reg.register_capability(
            LifecycleEntry(
                meta=CapabilityClassMetadata(name="B", depends_on=("A",)),
                instance=CapB(),
                source_path=Path("/b.py"),
                scan_root="builtins",
            )
        )
        await reg.register_capability(
            LifecycleEntry(
                meta=CapabilityClassMetadata(name="C", depends_on=("B",)),
                instance=CapC(),
                source_path=Path("/c.py"),
                scan_root="builtins",
            )
        )

        loader = CapabilityLoader(scan_roots=list(four_roots.items()), registry=reg)
        with pytest.raises(RuntimeError, match="boom"):
            await loader.start_lifecycles()

        # A was set up, B failed mid-setup, C never ran. Teardown:
        # A is rolled back. B and C don't teardown (B did not finish
        # setup; C did not start).
        assert order == ["setup:A", "setup:B", "teardown:A"]
