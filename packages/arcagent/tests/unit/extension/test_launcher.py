"""ProcessLauncher and SandboxPolicy — one execution path at every tier (COMP-008).

REQ-292 is the unusual requirement here: not "confine the process" but "there
SHALL NOT be a separate execution path for any tier". That is what makes federal
hardening a stringency dial rather than a rewrite, so it is asserted two ways —
behaviourally, by driving the same launcher with a personal no-op policy and
with a policy that visibly confines, and structurally, by reading
``ProcessLauncher``'s own source and failing if a tier name appears in it at all.
A second code path introduced "just for federal" fails this file.

REQ-273 (interpreter-startup and dynamic-loader variables scrubbed) is asserted
against the ACTUAL child environment, never against a call argument. The child
is a real Python that prints ``os.environ`` — an implementation that builds a
clean env dict and then forgets to pass it, or passes it and is overridden by
``pass_fds``/inheritance, fails here and would pass any mock. ``LD_PRELOAD`` and
``DYLD_INSERT_LIBRARIES`` load attacker code into a process Arc spawned on an
extension's behalf; ``PYTHONSTARTUP`` and ``NODE_OPTIONS`` do the same one layer
up, in the interpreter the extension's own runtime is about to start.

REQ-272 (lazy start, idle reap) is asserted against real processes and real
exit codes: a reaped process must be *stopped*, not merely dropped from a dict.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

from arcagent.core.tier import Tier

if TYPE_CHECKING:
    from arcagent.extension.launcher import ProcessLauncher

#: A child that reports the environment it was actually given, then exits.
_PRINT_ENV = "import json, os, sys; sys.stdout.write(json.dumps(dict(os.environ)))"

#: A child that stays alive long enough to be found running, reused, and reaped.
_STAY_ALIVE = "import time; time.sleep(30)"

#: Variables that let a third party inject code into a process we spawned for
#: them — the dynamic loader (LD_*/DYLD_*) and the interpreter (PYTHONSTARTUP,
#: NODE_OPTIONS). REQ-273 removes every one of them from the child.
_POISON = {
    "LD_PRELOAD": "/tmp/evil.so",
    "LD_LIBRARY_PATH": "/tmp/evil",
    "LD_AUDIT": "/tmp/audit.so",
    "DYLD_INSERT_LIBRARIES": "/tmp/evil.dylib",
    "DYLD_LIBRARY_PATH": "/tmp/evil",
    "PYTHONSTARTUP": "/tmp/evil.py",
    "NODE_OPTIONS": "--require /tmp/evil.js",
}

#: Nothing in the launcher may name a tier. Tier picks a *policy value*; it must
#: never pick a code path.
_TIER_WORDS = ("federal", "enterprise", "personal", "tier")


def _module() -> ModuleType:
    import arcagent.extension.launcher as module

    return module


def _definition(key: str, script: str, **overrides: Any) -> Any:
    module = _module()
    return module.ProcessDefinition(
        key=key,
        argv=[sys.executable, "-c", script],
        **overrides,
    )


@pytest.fixture
async def launcher() -> Any:
    """A launcher at the default (personal) policy, always torn down."""
    module = _module()
    instance = module.ProcessLauncher(policy=module.sandbox_policy_for(Tier.PERSONAL))
    try:
        yield instance
    finally:
        await instance.shutdown()


async def _child_environment(launcher: ProcessLauncher, definition: Any) -> dict[str, str]:
    """Start the env-printing child and return the environment it really got."""
    handle = await launcher.acquire(definition)
    stdout, _ = await asyncio.wait_for(handle.process.communicate(), timeout=30)
    parsed: dict[str, str] = json.loads(stdout.decode())
    return parsed


# --- REQ-273: the child's real environment ----------------------------------


async def test_the_child_environment_has_no_loader_or_interpreter_variables(
    launcher: ProcessLauncher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted on the child itself — a scrubbed dict that never got passed fails here."""
    for name, value in _POISON.items():
        monkeypatch.setenv(name, value)

    environment = await _child_environment(launcher, _definition("env", _PRINT_ENV))

    assert [name for name in _POISON if name in environment] == []


async def test_an_extension_cannot_reintroduce_a_scrubbed_variable(
    launcher: ProcessLauncher,
) -> None:
    """The scrub is the last word: a manifest's own env cannot smuggle LD_PRELOAD back."""
    definition = _definition("env", _PRINT_ENV, env={"LD_PRELOAD": "/tmp/evil.so"})

    environment = await _child_environment(launcher, definition)

    assert "LD_PRELOAD" not in environment


async def test_every_variable_matching_a_scrubbed_prefix_is_removed(
    launcher: ProcessLauncher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loader reads a family, not a fixed list — a new LD_* must not slip through."""
    monkeypatch.setenv("LD_BIND_NOW", "1")
    monkeypatch.setenv("DYLD_FRAMEWORK_PATH", "/tmp/evil")

    environment = await _child_environment(launcher, _definition("env", _PRINT_ENV))

    assert [name for name in environment if name.startswith(("LD_", "DYLD_"))] == []


async def test_the_child_keeps_what_it_needs_to_run(launcher: ProcessLauncher) -> None:
    """Scrubbing is surgical: an empty environment would break every extension."""
    definition = _definition("env", _PRINT_ENV, env={"ARC_EXTENSION_TOKEN_ID": "abc123"})

    environment = await _child_environment(launcher, definition)

    assert environment.get("PATH")
    assert environment.get("ARC_EXTENSION_TOKEN_ID") == "abc123"


# --- REQ-292: one path, policy is the only variable -------------------------


def test_personal_resolves_to_a_no_op_policy() -> None:
    """Zero confinement by default, and it is still a policy object, not a bypass."""
    module = _module()

    policy = module.sandbox_policy_for(Tier.PERSONAL)

    assert isinstance(policy, module.SandboxPolicy)
    assert policy.wrap(["a", "b"]) == ["a", "b"]


def test_every_tier_resolves_to_a_policy_through_the_same_seam() -> None:
    """No tier is unsupported and none is special-cased — each is one lookup."""
    module = _module()

    for tier in Tier:
        assert isinstance(module.sandbox_policy_for(tier), module.SandboxPolicy)


def test_the_launcher_never_branches_on_tier() -> None:
    """The structural half of REQ-292 — a second path cannot hide behind green tests."""
    module = _module()
    source = inspect.getsource(module.ProcessLauncher).lower()

    assert [word for word in _TIER_WORDS if word in source] == []


async def test_a_confining_policy_shapes_the_real_launch_through_the_same_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stricter policy changes the process, not the code path that starts it.

    The policy wraps argv in ``env ARC_SANDBOX=1 …``, which shows up in the
    child's own environment — proof the confinement took effect on a really
    launched process, through the same ``acquire`` every tier uses.
    """
    module = _module()

    class _ConfiningPolicy:
        name = "test-confinement"

        def wrap(self, argv: list[str]) -> list[str]:
            return ["/usr/bin/env", "ARC_SANDBOX=1", *argv]

    launcher = module.ProcessLauncher(policy=_ConfiningPolicy())
    try:
        environment = await _child_environment(launcher, _definition("env", _PRINT_ENV))
    finally:
        await launcher.shutdown()

    assert environment.get("ARC_SANDBOX") == "1"


# --- REQ-272: lazy start, reuse, idle reap ----------------------------------


async def test_no_process_starts_until_a_tool_needs_one(launcher: ProcessLauncher) -> None:
    """Constructing a launcher and holding a definition must cost nothing."""
    _definition("idle", _STAY_ALIVE)

    assert launcher.running == ()


async def test_the_first_acquire_starts_the_process(launcher: ProcessLauncher) -> None:
    handle = await launcher.acquire(_definition("idle", _STAY_ALIVE))

    assert launcher.running == ("idle",)
    assert handle.process.returncode is None


async def test_a_second_acquire_reuses_the_running_process(launcher: ProcessLauncher) -> None:
    """Restarting per call would defeat the point of a long-lived attachment."""
    definition = _definition("idle", _STAY_ALIVE)

    first = await launcher.acquire(definition)
    second = await launcher.acquire(definition)

    assert second.process.pid == first.process.pid
    assert launcher.running == ("idle",)


async def test_an_idle_process_is_actually_stopped_not_just_forgotten(
    launcher: ProcessLauncher,
) -> None:
    """Dropping the handle would leak a process per connection, per agent."""
    handle = await launcher.acquire(_definition("idle", _STAY_ALIVE, idle_timeout_seconds=0.05))
    await asyncio.sleep(0.2)

    reaped = await launcher.reap_idle()

    assert reaped == ("idle",)
    assert launcher.running == ()
    await asyncio.wait_for(handle.process.wait(), timeout=10)
    assert handle.process.returncode is not None


async def test_a_process_inside_its_idle_window_is_not_reaped(
    launcher: ProcessLauncher,
) -> None:
    handle = await launcher.acquire(_definition("idle", _STAY_ALIVE, idle_timeout_seconds=30))

    assert await launcher.reap_idle() == ()
    assert launcher.running == ("idle",)
    assert handle.process.returncode is None


async def test_acquiring_again_after_a_reap_starts_a_fresh_process(
    launcher: ProcessLauncher,
) -> None:
    definition = _definition("idle", _STAY_ALIVE, idle_timeout_seconds=0.05)
    first = await launcher.acquire(definition)
    await asyncio.sleep(0.2)
    await launcher.reap_idle()

    second = await launcher.acquire(definition)

    assert second.process.pid != first.process.pid
    assert launcher.running == ("idle",)


async def test_shutdown_stops_every_running_process(launcher: ProcessLauncher) -> None:
    first = await launcher.acquire(_definition("one", _STAY_ALIVE))
    second = await launcher.acquire(_definition("two", _STAY_ALIVE))

    await launcher.shutdown()

    assert launcher.running == ()
    for handle in (first, second):
        await asyncio.wait_for(handle.process.wait(), timeout=10)
        assert handle.process.returncode is not None


async def test_two_definitions_get_two_processes(launcher: ProcessLauncher) -> None:
    """Keys are per-connection; one extension's process is never handed to another."""
    first = await launcher.acquire(_definition("one", _STAY_ALIVE))
    second = await launcher.acquire(_definition("two", _STAY_ALIVE))

    assert first.process.pid != second.process.pid
    assert sorted(launcher.running) == ["one", "two"]


def test_sandbox_policy_is_a_contract_not_a_concrete_class() -> None:
    """Confinement is swapped by supplying an object, never by editing the launcher."""
    module = _module()

    class _Custom:
        name = "custom"

        def wrap(self, argv: list[str]) -> list[str]:
            return list(argv)

    assert isinstance(_Custom(), module.SandboxPolicy)
