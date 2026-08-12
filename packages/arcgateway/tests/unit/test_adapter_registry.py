"""Tests for the adapter registry (arcgateway.adapters.registry).

The registry is the gateway-core nucleus that discovers platform adapters by
scanning ``arcgateway/adapters/`` for a module-level ``PLATFORM`` descriptor
(SPEC-065 REQ-308), applies the four-pillar Authorize/Audit gate, and builds
enabled platforms generically.

The *scan itself* is covered by ``tests/adapters/test_registry_scan.py`` and
``test_registry_resilience.py``, which drive the real directory. This file
covers the build half — config gating, tier policy and identity resolution —
by substituting a synthetic roster, so it proves the builder is fully agnostic
without depending on which platforms happen to be in the tree.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from arcgateway.adapters import registry
from arcgateway.adapters.registry import (
    AdapterBuildContext,
    AdapterSpec,
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
    def to_parts(self, payload):  # type: ignore[no-untyped-def]
        return []

    async def send(self, target, parts, *, reply_to=None) -> None: ...  # type: ignore[no-untyped-def]
    async def send_with_id(self, target, message) -> str | None:  # type: ignore[no-untyped-def]
        return None


async def _noop_on_message(event) -> None:  # type: ignore[no-untyped-def]
    return None


def _spec(name: str, build=None) -> AdapterSpec:  # type: ignore[no-untyped-def]
    """A platform descriptor of the shape a folder's ``PLATFORM`` would be."""

    def _default_build(ctx: AdapterBuildContext) -> _FakeAdapter:
        return _FakeAdapter(name=ctx.name, agent_did=ctx.agent_did())

    return AdapterSpec(
        name=name,
        requires=(),
        supports=("text",),
        build=build or _default_build,
    )


@pytest.fixture
def roster(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Stand in for the directory scan with an explicit list of platforms.

    Substituting the scan rather than writing folders keeps these tests about
    the *builder*: which blocks it builds, which it refuses, and what it
    audits. Whether a folder is found at all is the scan suite's business.
    """

    def _install(specs: Iterable[AdapterSpec]) -> None:
        found = list(specs)
        monkeypatch.setattr(registry, "discover_adapters", lambda: found)

    return _install


@pytest.fixture(autouse=True)
def _empty_roster_by_default(roster) -> None:  # type: ignore[no-untyped-def]
    """No platform exists unless a test says so — including the real ones.

    Without this the in-tree telegram/slack/mattermost folders would leak into
    every case, and "the block's platform is not installed" would be untestable
    for exactly the names operators actually configure.
    """
    roster([])


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


def test_context_require_pairing_defaults_false() -> None:
    """Omitting require_pairing preserves every pre-existing caller's behaviour."""
    ctx = AdapterBuildContext(
        name="telegram",
        raw_config={"enabled": True},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
    )
    assert ctx.require_pairing is False


# ── the descriptor itself ────────────────────────────────────────────────────


def test_spec_declares_requirements_and_capabilities() -> None:
    """A platform says what it needs and what it can carry (COMP-004)."""
    spec = _spec("telegram")
    assert spec.requires == ()
    assert spec.supports == ("text",)


# ── build_adapters: require_pairing plumbing ─────────────────────────────────


def test_build_adapters_forwards_require_pairing_to_context(roster) -> None:  # type: ignore[no-untyped-def]
    """[security].require_pairing reaches each platform's AdapterBuildContext."""
    seen_ctx: list[AdapterBuildContext] = []

    def _build(ctx: AdapterBuildContext) -> _FakeAdapter:
        seen_ctx.append(ctx)
        return _FakeAdapter(name=ctx.name, agent_did=ctx.agent_did())

    roster([_spec("telegram", _build)])
    build_adapters(
        platforms={"telegram": {"enabled": True}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
        require_pairing=True,
    )

    assert len(seen_ctx) == 1
    assert seen_ctx[0].require_pairing is True


def test_build_adapters_defaults_require_pairing_false(roster) -> None:  # type: ignore[no-untyped-def]
    """Omitting require_pairing at build_adapters() preserves the disabled default."""
    seen_ctx: list[AdapterBuildContext] = []

    def _build(ctx: AdapterBuildContext) -> _FakeAdapter:
        seen_ctx.append(ctx)
        return _FakeAdapter(name=ctx.name, agent_did=ctx.agent_did())

    roster([_spec("telegram", _build)])
    build_adapters(
        platforms={"telegram": {"enabled": True}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
    )

    assert seen_ctx[0].require_pairing is False


# ── build_adapters: shared-default agent_did visibility (task 27) ───────────


def test_build_adapters_warns_when_platform_has_no_agent_did_override(
    monkeypatch: pytest.MonkeyPatch,
    roster,  # type: ignore[no-untyped-def]
) -> None:
    """A platform with no per-block agent_did silently inherits
    [gateway].agent_did — the exact mechanism that let a live gateway.toml
    rewrite (e.g. a deploy-node.sh re-run against a shared config while the
    service later restarts) silently repoint an already-live Telegram bot
    at a different agent with zero operator visibility. Emitting an audit
    event here doesn't prevent the rewrite, but makes the risk observable
    instead of silent."""
    events: list[tuple[str, str, str, dict[str, object]]] = []

    def _fake_emit(*, action, target, outcome, extra):  # type: ignore[no-untyped-def]
        events.append((action, target, outcome, extra))

    monkeypatch.setattr("arcgateway.adapters.registry.emit_event", _fake_emit)

    roster([_spec("telegram")])
    build_adapters(
        platforms={"telegram": {"enabled": True}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
    )

    warn_events = [e for e in events if e[0] == "gateway.adapter.shared_default_agent_did"]
    assert warn_events, "must audit when a platform inherits the shared gateway agent_did"
    assert warn_events[0][2] == "warn"


def test_build_adapters_no_warning_when_platform_has_own_agent_did(
    monkeypatch: pytest.MonkeyPatch,
    roster,  # type: ignore[no-untyped-def]
) -> None:
    """A platform with its own explicit agent_did override is not warned about."""
    events: list[tuple[str, str, str, dict[str, object]]] = []

    def _fake_emit(*, action, target, outcome, extra):  # type: ignore[no-untyped-def]
        events.append((action, target, outcome, extra))

    monkeypatch.setattr("arcgateway.adapters.registry.emit_event", _fake_emit)

    roster([_spec("telegram")])
    build_adapters(
        platforms={"telegram": {"enabled": True, "agent_did": "did:arc:agent:telegram-only"}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
    )

    warn_events = [e for e in events if e[0] == "gateway.adapter.shared_default_agent_did"]
    assert not warn_events


# ── build_adapters: enable filtering ─────────────────────────────────────────


def test_disabled_block_is_skipped(roster) -> None:  # type: ignore[no-untyped-def]
    roster([_spec("telegram")])
    adapters = build_adapters(
        platforms={"telegram": {"enabled": False}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
    )
    assert adapters == []


def test_enabled_block_builds_adapter(roster) -> None:  # type: ignore[no-untyped-def]
    roster([_spec("telegram")])
    adapters = build_adapters(
        platforms={"telegram": {"enabled": True, "agent_did": "did:arc:agent:tg"}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
    )
    assert [a.name for a in adapters] == ["telegram"]
    assert adapters[0].agent_did == "did:arc:agent:tg"  # type: ignore[attr-defined]


def test_invalid_platform_name_is_skipped_not_fatal_personal() -> None:
    adapters = build_adapters(
        platforms={"Bad-Name": {"enabled": True}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
    )
    assert adapters == []


# ── build_adapters: absent folder / tier policy ──────────────────────────────


def test_absent_official_platform_skips_at_personal() -> None:
    """REQ-309: an enabled platform with no folder leaves startup running."""
    adapters = build_adapters(
        platforms={"telegram": {"enabled": True}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
    )
    assert adapters == []


def test_absent_official_platform_is_fatal_at_federal() -> None:
    with pytest.raises(AdapterUnavailableError):
        build_adapters(
            platforms={"telegram": {"enabled": True}},
            on_message=_noop_on_message,
            default_agent_did="did:arc:agent:default",
            tier="federal",
        )


def test_unofficial_platform_loads_at_personal(roster) -> None:  # type: ignore[no-untyped-def]
    roster([_spec("customchat")])
    adapters = build_adapters(
        platforms={"customchat": {"enabled": True}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
    )
    assert [a.name for a in adapters] == ["customchat"]


def test_unofficial_platform_blocked_at_federal(roster) -> None:  # type: ignore[no-untyped-def]
    roster([_spec("customchat")])
    with pytest.raises(AdapterUnavailableError):
        build_adapters(
            platforms={"customchat": {"enabled": True}},
            on_message=_noop_on_message,
            default_agent_did="did:arc:agent:default",
            tier="federal",
        )


# ── build_adapters: build() raising (creds/dep missing) ──────────────────────


def test_build_raising_unavailable_skips_at_personal(roster) -> None:  # type: ignore[no-untyped-def]
    def _build(ctx: AdapterBuildContext):  # type: ignore[no-untyped-def]
        raise AdapterUnavailableError("TELEGRAM_BOT_TOKEN not set")

    roster([_spec("telegram", _build)])
    adapters = build_adapters(
        platforms={"telegram": {"enabled": True}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
    )
    assert adapters == []


def test_build_raising_unavailable_is_fatal_at_federal(roster) -> None:  # type: ignore[no-untyped-def]
    def _build(ctx: AdapterBuildContext):  # type: ignore[no-untyped-def]
        raise AdapterUnavailableError("token missing")

    roster([_spec("telegram", _build)])
    with pytest.raises(AdapterUnavailableError):
        build_adapters(
            platforms={"telegram": {"enabled": True}},
            on_message=_noop_on_message,
            default_agent_did="did:arc:agent:default",
            tier="federal",
        )


def test_build_raising_import_error_skips_at_personal(roster) -> None:  # type: ignore[no-untyped-def]
    def _build(ctx: AdapterBuildContext):  # type: ignore[no-untyped-def]
        raise ImportError("python-telegram-bot not installed")

    roster([_spec("telegram", _build)])
    adapters = build_adapters(
        platforms={"telegram": {"enabled": True}},
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
    )
    assert adapters == []


def test_multiple_platforms_build_in_order(roster) -> None:  # type: ignore[no-untyped-def]
    roster([_spec("telegram"), _spec("slack"), _spec("mattermost")])
    adapters = build_adapters(
        platforms={
            "telegram": {"enabled": True},
            "slack": {"enabled": False},
            "mattermost": {"enabled": True},
        },
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
    )
    assert [a.name for a in adapters] == ["telegram", "mattermost"]


# ── multi-bot: one platform, many blocks (1 bot per agent) ───────────────────


def test_multiple_blocks_reuse_one_platform_via_platform_key(roster) -> None:  # type: ignore[no-untyped-def]
    """Two `platform = "telegram"` blocks each build a telegram adapter for their agent."""
    roster([_spec("telegram")])
    adapters = build_adapters(
        platforms={
            "sales_telegram": {
                "enabled": True,
                "platform": "telegram",
                "agent_did": "did:arc:local:executor/sales",
            },
            "josh_telegram": {
                "enabled": True,
                "platform": "telegram",
                "agent_did": "did:arc:local:executor/josh",
            },
        },
        on_message=_noop_on_message,
        default_agent_did="did:arc:agent:default",
        tier="personal",
    )
    assert len(adapters) == 2
    assert {a.agent_did for a in adapters} == {
        "did:arc:local:executor/sales",
        "did:arc:local:executor/josh",
    }


def test_plain_block_name_still_resolves_without_platform_key(roster) -> None:  # type: ignore[no-untyped-def]
    """A block literally named `telegram` needs no `platform` key."""
    roster([_spec("telegram")])
    adapters = build_adapters(
        platforms={"telegram": {"enabled": True, "agent_did": "did:x"}},
        on_message=_noop_on_message,
        default_agent_did="did:default",
        tier="personal",
    )
    assert len(adapters) == 1


def test_unofficial_platform_key_blocked_at_federal(roster) -> None:  # type: ignore[no-untyped-def]
    """The federal allowlist check applies to the RESOLVED platform, not the block name."""
    roster([_spec("rogue")])
    with pytest.raises(AdapterUnavailableError):
        build_adapters(
            platforms={"sales_bot": {"enabled": True, "platform": "rogue"}},
            on_message=_noop_on_message,
            default_agent_did="did:x",
            tier="federal",
        )
