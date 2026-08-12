"""T-938 — a missing or broken adapter never blocks startup.

SPEC-065 REQ-309, COMP-003. Two independent failure modes, tested separately
because they fail in different places:

* **Absent** — a platform is enabled in config but its folder is not there.
  Startup skips it.
* **Broken** — a folder is there and raises on import. Startup skips it, keeps
  every other platform, and says so.

The second one is the supply-chain case (ASI04). A third-party adapter dropped
into the tree is untrusted code executed at import time; if its ``raise`` can
reach the gateway's startup path, any broken or hostile folder is a denial of
service against the whole daemon and every other platform on it. Availability
is the security property here, which is why the assertions are "the others
still loaded" and "the failure was recorded", not merely "no exception".

"Log the roster it did load" is read as one line naming every loaded platform —
an operator diagnosing a silent platform needs the roster in one place, not
reassembled from per-adapter lines.
"""

from __future__ import annotations

import importlib
import logging
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import arcgateway.adapters as adapters_pkg
from arcgateway.adapters import registry

_ADAPTERS_DIR = Path(adapters_pkg.__file__).parent

_REQUIRED_PLATFORMS = {"telegram", "slack", "mattermost"}


def _names(specs: Any) -> set[str]:
    return {getattr(spec, "name", "") for spec in specs}


@pytest.fixture
def broken_folder() -> Iterator[str]:
    """An adapter folder that raises at import time, removed in teardown."""
    name = f"broken_{uuid.uuid4().hex[:8]}"
    folder = _ADAPTERS_DIR / name
    folder.mkdir()
    (folder / "__init__.py").write_text(
        '"""An adapter that cannot be imported."""\n'
        'raise RuntimeError("this adapter is broken on purpose")\n',
        encoding="utf-8",
    )
    importlib.invalidate_caches()
    try:
        yield name
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        importlib.invalidate_caches()


# --- Broken folder -----------------------------------------------------------


def test_a_folder_that_raises_on_import_does_not_abort_discovery(
    broken_folder: str,
) -> None:
    """One bad folder must not be able to take the daemon down."""
    specs = registry.discover_adapters()

    found = _names(specs)
    assert broken_folder not in found, "the broken adapter was loaded anyway"


def test_the_other_platforms_still_load_alongside_a_broken_one(
    broken_folder: str,
) -> None:
    """Blast radius is the one folder, not the roster."""
    found = _names(registry.discover_adapters())

    missing = _REQUIRED_PLATFORMS - found
    assert not missing, (
        f"a single broken adapter folder took {sorted(missing)} down with it — "
        "one third-party folder is a denial of service against every platform"
    )


def test_the_loaded_roster_is_logged(
    broken_folder: str, caplog: pytest.LogCaptureFixture
) -> None:
    """One line names every platform that did load.

    Without it, a platform that silently stopped working is indistinguishable
    from one nobody configured.
    """
    with caplog.at_level(logging.INFO, logger="arcgateway.adapters.registry"):
        loaded = _names(registry.discover_adapters())

    roster_lines = [
        record.getMessage()
        for record in caplog.records
        if all(name in record.getMessage() for name in loaded)
    ]
    assert roster_lines, (
        f"no single log line names the loaded roster {sorted(loaded)}; "
        f"lines seen: {[r.getMessage() for r in caplog.records]}"
    )


def test_the_broken_adapter_is_recorded_not_swallowed(
    broken_folder: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An import failure is auditable (ASI04), not a silent skip."""
    events: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> None:
        events.append(kwargs)

    monkeypatch.setattr(registry, "emit_event", _capture)

    registry.discover_adapters()

    named = [event for event in events if broken_folder in str(event)]
    assert named, (
        f"importing {broken_folder!r} failed and emitted no audit event — an "
        f"adapter that vanishes on import leaves no trace. events: {events}"
    )


def test_a_broken_folder_does_not_break_the_next_scan(broken_folder: str) -> None:
    """Discovery is repeatable: a failed import is not cached as a poisoned roster."""
    first = _names(registry.discover_adapters())
    second = _names(registry.discover_adapters())

    assert first == second, (
        f"the roster changed between two scans with a broken folder present: "
        f"{sorted(first)} then {sorted(second)}"
    )
    assert _REQUIRED_PLATFORMS <= second


# --- Absent folder -----------------------------------------------------------


def test_an_enabled_platform_with_no_folder_leaves_startup_running() -> None:
    """Config names a platform that is not in the tree. Personal tier skips it."""
    built = registry.build_adapters(
        platforms={"nosuchplatform": {"enabled": True}},
        on_message=_noop,
        default_agent_did="did:arc:agent:test",
        tier="personal",
    )

    assert built == [], "an absent platform produced an adapter"


def test_an_absent_platform_does_not_stop_a_present_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping the absent one must not skip the rest of the config block."""
    built_names: list[str] = []

    def _build(ctx: Any) -> Any:
        built_names.append(ctx.name)
        return _StubAdapter(ctx.name)

    spec = registry.AdapterSpec(
        name="present", requires=(), supports=(), build=_build
    )
    monkeypatch.setattr(registry, "discover_adapters", lambda: [spec])

    registry.build_adapters(
        platforms={
            "nosuchplatform": {"enabled": True},
            "present": {"enabled": True},
        },
        on_message=_noop,
        default_agent_did="did:arc:agent:test",
        tier="personal",
    )

    assert built_names == ["present"], (
        f"an absent platform stopped a present one from loading: {built_names}"
    )


class _StubAdapter:
    def __init__(self, name: str) -> None:
        self.name = name
        self.agent_did = ""


async def _noop(event: Any) -> None:
    return None
