"""Tests for the generic adapter-plugin registry (arcgateway.adapters.registry).

The registry is the gateway-core nucleus that loads platform adapters from
separately-installed extension packages via entry points, applies the
four-pillar Authorize/Audit gate, and builds enabled adapters generically.
No platform-specific code lives in the gateway core — these tests prove the
loader is fully agnostic by driving it with synthetic plugins.
"""

from __future__ import annotations

import pytest

from arcgateway.adapters.registry import (
    AdapterBuildContext,
    AdapterPlugin,
    AdapterUnavailableError,
    build_adapters,
    validate_adapter_name,
)


class _FakeAdapter:
    """Minimal BasePlatformAdapter stand-in (only ``name`` is read here)."""

    def __init__(self, name: str, agent_did: str) -> None:
        self.name = name
        self.agent_did = agent_did

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def send(self, target, message, *, reply_to=None) -> None: ...  # type: ignore[no-untyped-def]
    async def send_with_id(self, target, message) -> str | None:  # type: ignore[no-untyped-def]
        return None


async def _noop_on_message(event) -> None:  # type: ignore[no-untyped-def]
    return None


def _plugin(name: str) -> AdapterPlugin:
    def _build(ctx: AdapterBuildContext) -> _FakeAdapter:
        return _FakeAdapter(name=ctx.name, agent_did=ctx.agent_did())

    return AdapterPlugin(name=name, build=_build)


# ── name validation ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["telegram", "slack", "mattermost", "x", "a1_b2"])
def test_validate_adapter_name_accepts_valid(name: str) -> None:
    validate_adapter_name(name)  # must not raise


@pytest.mark.parametrize(
    "name",
    ["", "Telegram", "1telegram", "../evil", "os.system", "a-b", "a" * 33],
)
def test_validate_adapter_name_rejects_invalid(name: str) -> None:
    with pytest.raises(ValueError):
        validate_adapter_name(name)


# ── build context agent_did resolution ───────────────────────────────────────


def test_context_agent_did_prefers_block_override() -> None:
    ctx = AdapterBuildContext(
        name="telegram",
        raw_config={"enabled": True, "agent_did": "did:arc:agent:override"},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
    )
    assert ctx.agent_did() == "did:arc:agent:override"


def test_context_agent_did_falls_back_to_default() -> None:
    ctx = AdapterBuildContext(
        name="telegram",
        raw_config={"enabled": True},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
    )
    assert ctx.agent_did() == "did:arc:agent:default"


# ── build_adapters: enable filtering ─────────────────────────────────────────


def test_disabled_block_is_skipped() -> None:
    adapters = build_adapters(
        platforms={"telegram": {"enabled": False}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
        plugins={"telegram": _plugin("telegram")},
    )
    assert adapters == []


def test_enabled_block_builds_adapter() -> None:
    adapters = build_adapters(
        platforms={"telegram": {"enabled": True, "agent_did": "did:arc:agent:tg"}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
        plugins={"telegram": _plugin("telegram")},
    )
    assert [a.name for a in adapters] == ["telegram"]
    assert adapters[0].agent_did == "did:arc:agent:tg"  # type: ignore[attr-defined]


def test_invalid_platform_name_is_skipped_not_fatal_personal() -> None:
    adapters = build_adapters(
        platforms={"Bad-Name": {"enabled": True}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
        plugins={},
    )
    assert adapters == []


# ── build_adapters: missing plugin / tier policy ─────────────────────────────


def test_missing_official_plugin_skips_at_personal() -> None:
    adapters = build_adapters(
        platforms={"telegram": {"enabled": True}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
        plugins={},  # telegram not installed
    )
    assert adapters == []


def test_missing_official_plugin_is_fatal_at_federal() -> None:
    with pytest.raises(AdapterUnavailableError):
        build_adapters(
            platforms={"telegram": {"enabled": True}},
            on_message=_noop_on_message,
            default_agent_did="did:arc:agent:default",
            tier="federal",
            plugins={},  # telegram not installed → federal must refuse to start
        )


def test_unofficial_plugin_loads_at_personal() -> None:
    adapters = build_adapters(
        platforms={"customchat": {"enabled": True}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
        plugins={"customchat": _plugin("customchat")},
    )
    assert [a.name for a in adapters] == ["customchat"]


def test_unofficial_plugin_blocked_at_federal() -> None:
    with pytest.raises(AdapterUnavailableError):
        build_adapters(
            platforms={"customchat": {"enabled": True}},
            on_message=_noop_on_message,
            default_agent_did="did:arc:agent:default",
            tier="federal",
            plugins={"customchat": _plugin("customchat")},
        )


# ── build_adapters: plugin.build raising (creds/dep missing) ─────────────────


def test_build_raising_unavailable_skips_at_personal() -> None:
    def _build(ctx: AdapterBuildContext):  # type: ignore[no-untyped-def]
        raise AdapterUnavailableError("TELEGRAM_BOT_TOKEN not set")

    adapters = build_adapters(
        platforms={"telegram": {"enabled": True}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
        plugins={"telegram": AdapterPlugin(name="telegram", build=_build)},
    )
    assert adapters == []


def test_build_raising_unavailable_is_fatal_at_federal() -> None:
    def _build(ctx: AdapterBuildContext):  # type: ignore[no-untyped-def]
        raise AdapterUnavailableError("token missing")

    with pytest.raises(AdapterUnavailableError):
        build_adapters(
            platforms={"telegram": {"enabled": True}},
            on_message=_noop_on_message,
            default_agent_did="did:arc:agent:default",
            tier="federal",
            plugins={"telegram": AdapterPlugin(name="telegram", build=_build)},
        )


def test_build_raising_import_error_skips_at_personal() -> None:
    def _build(ctx: AdapterBuildContext):  # type: ignore[no-untyped-def]
        raise ImportError("python-telegram-bot not installed")

    adapters = build_adapters(
        platforms={"telegram": {"enabled": True}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
        plugins={"telegram": AdapterPlugin(name="telegram", build=_build)},
    )
    assert adapters == []


def test_multiple_platforms_build_in_order() -> None:
    adapters = build_adapters(
        platforms={
            "telegram": {"enabled": True},
            "slack": {"enabled": False},
            "mattermost": {"enabled": True},
        },
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
        plugins={
            "telegram": _plugin("telegram"),
            "slack": _plugin("slack"),
            "mattermost": _plugin("mattermost"),
        },
    )
    assert [a.name for a in adapters] == ["telegram", "mattermost"]
