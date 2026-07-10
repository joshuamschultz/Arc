"""Task #39 — the shared arccli registry builder must not spawn background tasks.

Live bug: `arc agent tools` dumped a RuntimeError traceback ("memory module
called before runtime is configured"). `build_capability_registry` (task
#29's shared seam for `arc agent tools` and `arc ext inspect`) drives
`CapabilityLoader.scan_and_register` over every ENABLED module's
`capabilities.py` — including `arcagent.modules.memory`, which registers a
real `@background_task` (`memory_consolidate_loop`) whose body depends on
`modules/memory/_runtime.configure()` having been called by a live agent's
startup. `CapabilityRegistry.register_task` used to spawn that task
immediately on registration, regardless of caller — a read-only CLI listing
command has no live agent, no configured module runtime, so the task's
first line raised the instant the scan registered it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arccli.commands._capability_registry import build_capability_registry


class _FakeModuleEntry:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled


def _config_with_memory_enabled() -> object:
    return SimpleNamespace(modules={"memory": _FakeModuleEntry(enabled=True)})


class TestBuildCapabilityRegistryDoesNotSpawn:
    def test_loader_constructed_with_spawn_background_tasks_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # build_capability_registry imports CapabilityLoader lazily (arccli's
        # cold-start-budget convention — see module docstring) so we patch
        # the SOURCE module's attribute; the local `from ... import` inside
        # the function resolves it fresh on every call.
        import arcagent.capabilities.capability_loader as loader_mod

        captured: dict[str, object] = {}
        real_init = loader_mod.CapabilityLoader.__init__

        def _spy_init(self: object, **kwargs: object) -> None:
            captured.update(kwargs)
            real_init(self, **kwargs)  # type: ignore[arg-type]

        class _SpyLoader(loader_mod.CapabilityLoader):
            __init__ = _spy_init  # type: ignore[assignment]

        monkeypatch.setattr(loader_mod, "CapabilityLoader", _SpyLoader)

        build_capability_registry(_config_with_memory_enabled(), agent_root=None)

        assert captured.get("spawn_background_tasks") is False

    def test_real_memory_module_scan_does_not_raise(self) -> None:
        """The literal live reproduction: scan with the memory module enabled
        (which registers a real @background_task) and prove it completes
        cleanly — no exception, no None-registry fallback from the
        `except Exception` degrade path in build_capability_registry.
        """
        registry = build_capability_registry(_config_with_memory_enabled(), agent_root=None)
        assert registry is not None
