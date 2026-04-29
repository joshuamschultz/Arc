"""SPEC-021 Task 3.7 — policy module decorator-form tests.

The new ``capabilities.py`` exposes three ``@hook``-decorated
functions. This file verifies:

  1. The three hooks register via :class:`CapabilityLoader` against the
     policy module directory.
  2. The hooks honor the same priorities as the legacy
     :class:`PolicyModule`.
  3. ``inject_policy_md`` writes ``policy.md`` content into the
     sections dict when the file exists.
  4. ``periodic_policy_eval`` skips automated runs (no session_id).

Legacy :class:`PolicyModule` tests in ``test_policy_module.py``
continue to verify behaviour at the wrapper level.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from arcagent.core.capability_loader import CapabilityLoader
from arcagent.core.capability_registry import CapabilityRegistry
from arcagent.modules.policy import _runtime


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


@pytest.fixture
def configured(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _runtime.configure(workspace=workspace, agent_name="test")
    return workspace


@pytest.mark.asyncio
class TestLoaderRegistration:
    async def test_three_hooks_register(self, tmp_path: Path) -> None:
        from arcagent.modules.policy import capabilities as policy_caps

        module_dir = Path(policy_caps.__file__).parent
        # Loader scans .py files in the directory; only capabilities.py
        # has @hook stamps among the policy files.
        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("policy", module_dir)], registry=reg)
        await loader.scan_and_register()

        prompt_hooks = await reg.get_hooks("agent:assemble_prompt")
        respond_hooks = await reg.get_hooks("agent:post_respond")
        shutdown_hooks = await reg.get_hooks("agent:shutdown")

        assert any(h.meta.name == "inject_policy_md" for h in prompt_hooks)
        assert any(h.meta.name == "periodic_policy_eval" for h in respond_hooks)
        assert any(h.meta.name == "terminal_policy_eval" for h in shutdown_hooks)

    async def test_hook_priorities_match_legacy(self) -> None:
        from arcagent.modules.policy.capabilities import (
            inject_policy_md,
            periodic_policy_eval,
            terminal_policy_eval,
        )

        # Same priorities as the original PolicyModule.startup() registrations.
        assert inject_policy_md._arc_capability_meta.priority == 60  # type: ignore[attr-defined]
        assert periodic_policy_eval._arc_capability_meta.priority == 110  # type: ignore[attr-defined]
        assert terminal_policy_eval._arc_capability_meta.priority == 60  # type: ignore[attr-defined]


@pytest.mark.asyncio
class TestInjectPolicyMd:
    async def test_writes_section_when_file_present(self, configured: Path) -> None:
        from arcagent.modules.policy.capabilities import inject_policy_md

        (configured / "policy.md").write_text("learned lessons here")
        sections: dict[str, str] = {}
        ctx = SimpleNamespace(data={"sections": sections})
        await inject_policy_md(ctx)
        assert sections["policy"] == "learned lessons here"

    async def test_skips_when_file_absent(self, configured: Path) -> None:
        from arcagent.modules.policy.capabilities import inject_policy_md

        sections: dict[str, str] = {}
        ctx = SimpleNamespace(data={"sections": sections})
        await inject_policy_md(ctx)
        assert "policy" not in sections


@pytest.mark.asyncio
class TestPeriodicPolicyEval:
    async def test_skips_when_no_session_id(self, configured: Path) -> None:
        """Background runs (no session_id) must not advance turn count."""
        from arcagent.modules.policy.capabilities import periodic_policy_eval

        ctx = SimpleNamespace(data={"messages": [{"role": "user"}]})
        await periodic_policy_eval(ctx)
        assert _runtime.state().turn_count == 0


@pytest.mark.asyncio
class TestRuntimeContract:
    async def test_unconfigured_raises(self) -> None:
        from arcagent.modules.policy.capabilities import inject_policy_md

        with pytest.raises(RuntimeError, match="before runtime is configured"):
            await inject_policy_md(SimpleNamespace(data={"sections": {}}))
