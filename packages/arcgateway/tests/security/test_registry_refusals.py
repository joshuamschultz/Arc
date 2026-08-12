"""The adapter registry's supply-chain gate, exercised branch by branch.

SPEC-065 review: every refusal in ``_load_descriptor`` and the federal-tier
raise in ``build_adapters`` was reachable and none was tested. That is the
ASI04 / LLM03 control point for "a platform is a folder" — the thing standing
between a dropped-in folder and code executing inside the daemon. A regression
that let any of these fall through to *loading* the adapter would have passed
the whole suite.

Each refusal is asserted on all three of its effects: the spec is not returned,
the reason is audited, and the daemon keeps going.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

from arcgateway.adapters import registry
from arcgateway.adapters.registry import AdapterSpec, AdapterUnavailableError


@pytest.fixture
def audited(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, str]]:
    """Capture every audit event the registry emits."""
    events: list[tuple[str, str, str, str]] = []

    def _capture(action: str, target: str, outcome: str, **extra: Any) -> None:
        events.append((action, target, outcome, str(extra.get("reason", ""))))

    monkeypatch.setattr(registry, "_audit", _capture)
    return events


@pytest.fixture
def planted(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Install a fake module under the adapters package, removed on teardown."""
    installed: list[str] = []

    def _plant(name: str, descriptor: Any) -> None:
        full = f"{registry._ADAPTERS_PACKAGE}.{name}"
        module = types.ModuleType(full)
        if descriptor is not None:
            module.PLATFORM = descriptor  # type: ignore[attr-defined]
        sys.modules[full] = module
        installed.append(full)

    yield _plant
    for full in installed:
        sys.modules.pop(full, None)


async def _build(_draft: Any) -> None:
    return None


def _spec(name: str) -> AdapterSpec:
    return AdapterSpec(name=name, requires=(), supports=(), build=_build)


# --- _load_descriptor refusals ---------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    ["../evil", "os.system", "Telegram", "9lives", "a" * 33, "has-a-dash"],
)
def test_a_folder_whose_name_is_not_a_platform_name_is_blocked(
    bad_name: str, audited: list[tuple[str, str, str, str]]
) -> None:
    """Name validation runs before the import, so hostile names never execute."""
    assert registry._load_descriptor(bad_name) is None
    assert ("gateway.adapter.blocked", bad_name, "deny", "invalid_name") in audited


def test_a_module_exporting_something_other_than_a_spec_is_blocked(
    planted: Any, audited: list[tuple[str, str, str, str]]
) -> None:
    """``PLATFORM`` naming the wrong type is a supply-chain signal, not a typo."""
    planted("impostor", {"name": "impostor"})

    assert registry._load_descriptor("impostor") is None
    assert ("gateway.adapter.blocked", "impostor", "deny", "not_an_adapter_spec") in audited


def test_a_spec_that_renames_itself_away_from_its_folder_is_blocked(
    planted: Any, audited: list[tuple[str, str, str, str]]
) -> None:
    """A platform is named by its folder; a spec claiming another name is not it.

    Without this the folder ``harmless`` could declare itself ``telegram`` and
    take over an official platform's configuration block.
    """
    planted("harmless", _spec("telegram"))

    assert registry._load_descriptor("harmless") is None
    assert ("gateway.adapter.blocked", "harmless", "deny", "name_mismatch") in audited


def test_a_module_with_no_descriptor_is_skipped_quietly(planted: Any) -> None:
    """base.py and registry.py live here too and are not platforms."""
    planted("helper", None)
    assert registry._load_descriptor("helper") is None


def test_a_private_module_is_skipped_without_importing_it() -> None:
    """``_text``/``_reconnect`` are shared helpers, never candidates."""
    assert registry._load_descriptor("_text") is None


def test_a_valid_folder_still_yields_its_spec(planted: Any) -> None:
    """The gate refuses; it does not merely refuse everything."""
    planted("goodplat", _spec("goodplat"))
    spec = registry._load_descriptor("goodplat")
    assert spec is not None
    assert spec.name == "goodplat"


# --- build_adapters tier behaviour -----------------------------------------


def test_federal_refuses_to_start_on_an_invalid_platform_name() -> None:
    """A federal deployment serves every adapter it declared, or none."""
    with pytest.raises(AdapterUnavailableError, match="invalid adapter name"):
        registry.build_adapters(
            platforms={"../evil": {"enabled": True}},
            on_message=_build,
            default_agent_did="did:arc:alpha",
            tier="federal",
        )


def test_personal_skips_an_invalid_platform_name_and_keeps_running(
    audited: list[tuple[str, str, str, str]],
) -> None:
    """Same input, other tier: availability wins over strictness."""
    adapters = registry.build_adapters(
        platforms={"../evil": {"enabled": True}},
        on_message=_build,
        default_agent_did="did:arc:alpha",
        tier="personal",
    )
    assert adapters == []
    assert ("gateway.adapter.blocked", "../evil", "deny", "invalid_name") in audited


def test_federal_refuses_a_platform_outside_the_signed_allowlist() -> None:
    """An unofficial adapter is a startup failure at federal, not a warning."""
    with pytest.raises(AdapterUnavailableError, match="federal allowlist"):
        registry.build_adapters(
            platforms={"someplat": {"enabled": True}},
            on_message=_build,
            default_agent_did="did:arc:alpha",
            tier="federal",
        )
