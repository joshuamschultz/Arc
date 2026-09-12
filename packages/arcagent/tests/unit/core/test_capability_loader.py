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
from types import SimpleNamespace

import pytest

from arcagent.capabilities.capability_registry import CapabilityRegistry


def _write_tool(path: Path, name: str, *, version: str = "1.0.0") -> None:
    """Materialise a one-tool .py file under ``path``."""
    body = (
        "from arcagent.tools._decorator import tool\n"
        f'@tool(description="t", version="{version}")\n'
        f"async def {name}() -> str:\n"
        '    return "ok"\n'
    )
    path.write_text(body)


def _write_background_task(path: Path, name: str) -> None:
    """Materialise a .py file whose @background_task raises if actually run —
    simulating a module (e.g. memory) that depends on a live agent's
    module-level `_runtime.configure()` having been called first.
    """
    body = (
        "from arcagent.tools._decorator import background_task\n"
        f'@background_task(name="{name}", interval=0.1)\n'
        "async def poll(ctx) -> None:\n"
        '    raise RuntimeError("module called before runtime is configured")\n'
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


class _RecordingBus:
    """Minimal module-bus double that records every ``emit`` the loader makes.

    The loader emits with keyword args (``emit(event=..., data=...)``); mirror
    that exactly so a real refusal event is observable without a live bus.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    async def emit(self, *, event: str, data: dict[str, object]) -> None:
        self.events.append((event, data))


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
        from arcagent.capabilities.capability_loader import CapabilityLoader

        _write_tool(four_roots["builtins"] / "echo.py", "echo")

        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=list(four_roots.items()), registry=reg)
        await loader.scan_and_register()

        entry = await reg.get_tool("echo")
        assert entry is not None
        assert entry.scan_root == "builtins"

    async def test_registers_a_skill(self, four_roots: dict[str, Path]) -> None:
        from arcagent.capabilities.capability_loader import CapabilityLoader

        _write_skill(four_roots["builtins"] / "create-tool", "create-tool")

        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=list(four_roots.items()), registry=reg)
        await loader.scan_and_register()

        skill = await reg.get_skill("create-tool")
        assert skill is not None
        assert skill.version == "1.0.0"


@pytest.mark.asyncio
class TestSpawnBackgroundTasks:
    """Task #39 — a read-only registry scan must not spawn background tasks.

    Live bug: `arc agent tools` (and `arc ext inspect`, and arcui's inventory
    seam) built a throwaway CapabilityLoader/CapabilityRegistry over the
    scan roots and called scan_and_register(). Registering a
    @background_task ALWAYS spawned it immediately (capability_registry.
    register_task -> asyncio.create_task) — including the memory module's
    consolidation loop, whose body depends on modules/memory/_runtime.
    configure() having been called by a live agent's startup. A read-only
    scan never calls that, so the task raised "called before runtime is
    configured" the instant registration happened.
    """

    async def test_spawn_background_tasks_false_registers_without_running_it(
        self, four_roots: dict[str, Path]
    ) -> None:
        from arcagent.capabilities.capability_loader import CapabilityLoader

        _write_background_task(four_roots["builtins"] / "poller.py", "poll")

        reg = CapabilityRegistry()
        loader = CapabilityLoader(
            scan_roots=list(four_roots.items()),
            registry=reg,
            spawn_background_tasks=False,
        )
        # Must not raise — the task's body (which raises RuntimeError) never runs.
        delta = await loader.scan_and_register()

        assert not delta.errors
        entry = await reg.get_task("poll")
        assert entry is not None
        assert entry.task is None

    async def test_spawn_background_tasks_true_is_still_the_default(
        self, four_roots: dict[str, Path]
    ) -> None:
        """Regression guard: a live agent's real loader (agent_lifecycle.py)
        never passes `spawn_background_tasks` — omitting it must still spawn.
        """
        import asyncio

        from arcagent.capabilities.capability_loader import CapabilityLoader

        body = (
            "from arcagent.tools._decorator import background_task\n"
            "import asyncio\n"
            '@background_task(name="poll_live", interval=0.1)\n'
            "async def poll(ctx) -> None:\n"
            "    await asyncio.sleep(10)\n"
        )
        (four_roots["builtins"] / "poller.py").write_text(body)

        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=list(four_roots.items()), registry=reg)
        await loader.scan_and_register()

        entry = await reg.get_task("poll_live")
        assert entry is not None
        assert entry.task is not None and not entry.task.done()

        entry.task.cancel()
        try:
            await entry.task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
class TestSkillsSubdirRoot:
    async def test_capabilities_skills_subdir_loads(self, four_roots: dict[str, Path]) -> None:
        """A skill at ``<root>/skills/<name>/SKILL.md`` (create_skill's target)
        loads when a ``<name>-skills`` root is registered — the loader scans the
        skills subdir the same way it scans ``builtins/skills``."""
        from arcagent.capabilities.capability_loader import CapabilityLoader

        skills_dir = four_roots["workspace"] / "skills"
        _write_skill(skills_dir / "authored", "authored")

        reg = CapabilityRegistry()
        scan_roots = [
            ("workspace", four_roots["workspace"]),
            ("workspace-skills", skills_dir),
        ]
        loader = CapabilityLoader(scan_roots=scan_roots, registry=reg)
        await loader.scan_and_register()

        skill = await reg.get_skill("authored")
        assert skill is not None
        assert skill.scan_root == "workspace-skills"


@pytest.mark.asyncio
class TestScanPrecedence:
    async def test_workspace_overrides_builtins(self, four_roots: dict[str, Path]) -> None:
        """When the same name appears in two roots, later-scanned wins."""
        from arcagent.capabilities.capability_loader import CapabilityLoader

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
        from arcagent.capabilities.capability_loader import CapabilityLoader

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
        from arcagent.capabilities.capability_loader import CapabilityLoader

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
        from arcagent.capabilities.capability_loader import CapabilityLoader

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
class TestTransactionalReload:
    async def test_invalid_candidate_preserves_live_tool(self, tmp_path: Path) -> None:
        from arcagent.capabilities.capability_loader import CapabilityLoader

        _write_tool(tmp_path / "stable.py", "stable")
        registry = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("builtins", tmp_path)], registry=registry)
        await loader.scan_and_register()
        original = await registry.get_tool("stable")

        (tmp_path / "stable.py").write_text("this is not valid python !!!")
        prepared = await loader.prepare_reload()

        assert prepared.delta.errors
        assert await registry.get_tool("stable") is original

    async def test_changed_hook_replaces_code_and_priority_exactly_once(self) -> None:
        from arcagent.capabilities.capability_registry import HookEntry
        from arcagent.core.agent_lifecycle import bridge_capability_hooks_to_bus
        from arcagent.core.module_bus import ModuleBus
        from arcagent.tools._decorator import HookMetadata

        calls: list[str] = []

        async def old(_ctx: object) -> None:
            calls.append("old")

        async def updated(_ctx: object) -> None:
            calls.append("updated")

        async def middle(_ctx: object) -> None:
            calls.append("middle")

        live = CapabilityRegistry()
        candidate = CapabilityRegistry()
        await live.register_hook(
            HookEntry(
                meta=HookMetadata(name="refresh", event="tick", priority=200),
                handler=old,
                source_path=Path("old.py"),
                scan_root="builtins",
            )
        )
        await candidate.register_hook(
            HookEntry(
                meta=HookMetadata(name="refresh", event="tick", priority=10),
                handler=updated,
                source_path=Path("updated.py"),
                scan_root="builtins",
            )
        )
        bus = ModuleBus()
        bus.subscribe("tick", middle, priority=100, module_name="module:middle")
        agent = SimpleNamespace(_capability_registry=live, _bus=bus)
        await bridge_capability_hooks_to_bus(agent)

        await live.replace_from(candidate)
        await bridge_capability_hooks_to_bus(agent)
        await bus.emit("tick", {})

        assert calls == ["updated", "middle"]
        assert bus.handler_count_by_module("tick", "capability:refresh") == 1


@pytest.mark.asyncio
class TestCapabilityLifecycleOrder:
    async def test_topo_setup_and_reverse_teardown(self, four_roots: dict[str, Path]) -> None:
        """A → B → C dependency chain. Setup A,B,C; shutdown C,B,A."""
        from arcagent.capabilities.capability_loader import CapabilityLoader

        order: list[str] = []

        # Class capabilities are written via test fixtures rather than
        # source files for isolation — they register through a hook.
        from arcagent.capabilities.capability_registry import LifecycleEntry
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
        from arcagent.capabilities.capability_loader import CapabilityLoader
        from arcagent.capabilities.capability_registry import LifecycleEntry
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


@pytest.mark.asyncio
class TestAntiShadowGuard:
    """SPEC-081 T-1060 / REQ-402 / COMP-003 — anti-shadow guard.

    A SKILL.md is injected into the agent prompt (LLM01/ASI06). A skill loaded
    from a NON-TRUSTED scan root that declares a ``name`` already owned by a
    TRUSTED built-in must be REFUSED — otherwise a later-scanned agent-writable
    root silently replaces a shipped built-in by last-wins precedence.

    ``builtins`` is a TRUSTED root (``_TRUSTED_ROOTS``); ``workspace`` and
    ``agent`` are UNTRUSTED (``root_trust`` default). The loader is bare (no
    TOFU / no signature floor), so the Sign/TOFU gate short-circuits to ALLOW
    and an untrusted skill would otherwise register today.
    """

    async def test_untrusted_skill_named_after_builtin_is_refused(
        self, four_roots: dict[str, Path]
    ) -> None:
        """RED: the built-in wins the name; the untrusted shadow never replaces it.

        Fails today because skills are last-wins by bare name — ``workspace`` is
        scanned after ``builtins`` and overwrites the built-in entry.
        """
        from arcagent.capabilities.capability_loader import CapabilityLoader

        _write_skill(four_roots["builtins"] / "humanize", "humanize", version="1.0.0")
        _write_skill(four_roots["workspace"] / "humanize", "humanize", version="9.9.9")

        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=list(four_roots.items()), registry=reg)
        await loader.scan_and_register()

        skill = await reg.get_skill("humanize")
        assert skill is not None
        # The TRUSTED built-in must remain the registered entry, unshadowed.
        assert skill.scan_root == "builtins"
        assert skill.version == "1.0.0"

    async def test_builtin_name_collision_emits_registration_refused(
        self, four_roots: dict[str, Path]
    ) -> None:
        """RED: the refusal is observable as a bus event with the collision reason.

        Fails today because no refusal path exists — the untrusted skill is
        accepted silently, so no ``capability:registration_refused`` is emitted.
        """
        from arcagent.capabilities.capability_loader import CapabilityLoader

        _write_skill(four_roots["builtins"] / "humanize", "humanize")
        _write_skill(four_roots["workspace"] / "humanize", "humanize")

        bus = _RecordingBus()
        reg = CapabilityRegistry()
        loader = CapabilityLoader(
            scan_roots=list(four_roots.items()), registry=reg, bus=bus
        )
        await loader.scan_and_register()

        refusals = [
            data for (event, data) in bus.events if event == "capability:registration_refused"
        ]
        assert refusals, (
            "expected a capability:registration_refused event for the shadowing skill; "
            f"saw events: {[e for e, _ in bus.events]}"
        )
        assert any(data.get("reason") == "builtin_name_collision" for data in refusals)
        # The refused skill's identity is observable in the payload.
        assert any("humanize" in str(data.values()) for data in refusals)

    async def test_non_colliding_untrusted_skill_still_registers(
        self, four_roots: dict[str, Path]
    ) -> None:
        """Scope guard: the refusal is NARROW — an untrusted skill with a name no
        built-in owns still registers normally, and the built-in is untouched.

        Green today and after; it fails only if T-1061 over-broadens the guard
        to refuse untrusted skills that do not actually collide.
        """
        from arcagent.capabilities.capability_loader import CapabilityLoader

        _write_skill(four_roots["builtins"] / "humanize", "humanize")
        _write_skill(four_roots["workspace"] / "my-note-taker", "my-note-taker")

        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=list(four_roots.items()), registry=reg)
        await loader.scan_and_register()

        authored = await reg.get_skill("my-note-taker")
        assert authored is not None
        assert authored.scan_root == "workspace"
        builtin = await reg.get_skill("humanize")
        assert builtin is not None
        assert builtin.scan_root == "builtins"

    async def test_untrusted_vs_untrusted_collision_stays_last_wins(
        self, four_roots: dict[str, Path]
    ) -> None:
        """Scope guard: the rule targets ONLY trusted-name shadowing.

        A collision between two NON-TRUSTED roots (``agent`` and ``workspace``)
        is out of scope and stays last-wins. Green today and after; it fails
        only if T-1061 wrongly extends the guard to same-tier collisions.
        """
        from arcagent.capabilities.capability_loader import CapabilityLoader

        _write_skill(four_roots["agent"] / "foo", "foo", version="1.0.0")
        _write_skill(four_roots["workspace"] / "foo", "foo", version="2.0.0")

        reg = CapabilityRegistry()
        scan_roots = [
            ("agent", four_roots["agent"]),
            ("workspace", four_roots["workspace"]),
        ]
        loader = CapabilityLoader(scan_roots=scan_roots, registry=reg)
        await loader.scan_and_register()

        skill = await reg.get_skill("foo")
        assert skill is not None
        assert skill.scan_root == "workspace"
        assert skill.version == "2.0.0"
