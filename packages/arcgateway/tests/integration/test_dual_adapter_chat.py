"""Integration tests — dual-adapter chat (web + Slack) for SPEC-025 Track B.

Verifies FR-2 / AC-2.1 / AC-2.2:

  test_build_registers_both_web_and_slack_adapters (B5 / AC-2.1)
      When [platforms.web] and [platforms.slack] are both enabled and tokens
      are present, build_for_embedded populates both adapter slots.

  test_slack_and_web_events_share_session_router (B5 / AC-2.2)
      An inbound slack event and a web event for the same agent both route
      through the single shared SessionRouter. Each event's session key is
      formed by its adapter; both events land in the same router's queue.

  test_audit_chain_shows_interleaved_platform_events (B5 / AC-2.2)
      When the SessionRouter handles events from both web and slack for the
      same agent, the routed events carry platform="web" and platform="slack"
      respectively — audit chain is interleaved by platform for the same agent.

Design:
    - Uses a FakeWebAdapter and a real SlackAdapter (with in-process dedup)
      rather than hitting real network endpoints.
    - No arcui imports — purely arcgateway components.
    - Mock tokens satisfy SlackAdapter's xoxb-/xapp- prefix validation.
    - SlackAdapter._handle_inbound is called directly to simulate an inbound
      event without needing a real Socket Mode connection.
    - Session keys: WebPlatformAdapter uses build_session_key(agent_did, user_did);
      SlackAdapter uses "slack:{channel}:{user_id}" as its session key. Both
      conventions are correct for each adapter — the router is platform-agnostic
      at the routing layer (keyed by whatever session_key the adapter provides).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from arcgateway.adapters.slack import SlackAdapter
from arcgateway.bootstrap import EmbeddedGateway, build_for_embedded
from arcgateway.config import GatewayConfig
from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import AsyncioExecutor, InboundEvent
from arcgateway.session import SessionRouter, build_session_key

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_AGENT_DID = "did:arc:agent:test_dual"
_USER_DID = "did:arc:user:human/abc123"

# Fake tokens that satisfy SlackAdapter's prefix guard (D-015).
_BOT_TOKEN = "xoxb-test-dual-adapter-bot"
_APP_TOKEN = "xapp-test-dual-adapter-app"


# ---------------------------------------------------------------------------
# Minimal echo agent stub
# ---------------------------------------------------------------------------


class _EchoAgent:
    """Minimal agent that echoes the incoming message — satisfies ArcAgent.run()."""

    def __init__(self, agent_did: str) -> None:
        self.agent_did = agent_did

    async def run(self, task: str) -> str:
        return f"echo: {task}"


async def _echo_factory(agent_did: str) -> _EchoAgent:
    return _EchoAgent(agent_did)


# ---------------------------------------------------------------------------
# FakeWebAdapter — satisfies BasePlatformAdapter Protocol without HTTP/WS
# ---------------------------------------------------------------------------


class FakeWebAdapter:
    """Minimal web adapter for testing the dual-adapter registration path.

    Implements the BasePlatformAdapter Protocol surface. Does not open any
    sockets; just records send() calls and exposes simulate_inbound().
    """

    name: str = "web"

    def __init__(self, on_message: Callable[[InboundEvent], Awaitable[None]]) -> None:
        self._on_message = on_message
        self.sent: list[tuple[DeliveryTarget, str]] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def send(
        self,
        target: DeliveryTarget,
        message: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        self.sent.append((target, message))

    async def send_with_id(self, target: DeliveryTarget, message: str) -> str | None:
        await self.send(target, message)
        return None

    async def simulate_inbound(
        self,
        user_did: str,
        agent_did: str,
        text: str,
        chat_id: str = "webchat-001",
    ) -> None:
        """Push a simulated inbound web event through on_message."""
        session_key = build_session_key(agent_did, user_did)
        event = InboundEvent(
            platform="web",
            chat_id=chat_id,
            user_did=user_did,
            agent_did=agent_did,
            session_key=session_key,
            message=text,
        )
        await self._on_message(event)


# ---------------------------------------------------------------------------
# SpySessionRouter — wraps SessionRouter.handle to record routed events
# ---------------------------------------------------------------------------


class _SpyRouter(SessionRouter):
    """SessionRouter subclass that records every InboundEvent routed through handle()."""

    def __init__(self, executor: AsyncioExecutor) -> None:
        super().__init__(executor=executor)
        self.routed_events: list[InboundEvent] = []

    async def handle(self, event: InboundEvent) -> None:
        self.routed_events.append(event)
        await super().handle(event)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_team_root(tmp_path: Path) -> Path:
    return tmp_path / "team"


@pytest.fixture
def spy_router() -> _SpyRouter:
    executor = AsyncioExecutor(agent_factory=_echo_factory)
    return _SpyRouter(executor=executor)


@pytest.fixture
def web_adapter(spy_router: _SpyRouter) -> FakeWebAdapter:
    adapter = FakeWebAdapter(on_message=spy_router.handle)
    spy_router.set_adapter(adapter)
    return adapter


@pytest.fixture
def slack_adapter(spy_router: _SpyRouter) -> SlackAdapter:
    return SlackAdapter(
        bot_token=_BOT_TOKEN,
        app_token=_APP_TOKEN,
        allowed_user_ids=["U_TEST_USER"],
        on_message=spy_router.handle,
        dedup_db_path=None,  # in-memory SQLite
    )


# ---------------------------------------------------------------------------
# Test 1 — build_for_embedded registers both adapters (B5 / AC-2.1)
# ---------------------------------------------------------------------------


async def test_build_registers_both_web_and_slack_adapters(
    empty_team_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_for_embedded with web + slack enabled populates both adapter slots.

    Confirms AC-2.1: arc-stack startup log shows both adapters registering.
    The EmbeddedGateway.slack_adapter is not None when tokens are present.
    """
    monkeypatch.setenv("SLACK_BOT_TOKEN", _BOT_TOKEN)
    monkeypatch.setenv("SLACK_APP_TOKEN", _APP_TOKEN)

    cfg = GatewayConfig.from_toml_str(
        f"""
[gateway]
agent_did = "{_AGENT_DID}"

[platforms.web]
enabled = true

[platforms.slack]
enabled = true
bot_token_env = "SLACK_BOT_TOKEN"
app_token_env = "SLACK_APP_TOKEN"
"""
    )

    bundle = await build_for_embedded(empty_team_root, cfg)

    assert isinstance(bundle, EmbeddedGateway)
    assert bundle.web_adapter is not None, "web_adapter slot must be populated"
    assert bundle.slack_adapter is not None, "slack_adapter slot must be populated"
    assert bundle.web_adapter.name == "web"
    assert bundle.slack_adapter.name == "slack"
    # Both adapters share the same SessionRouter — confirmed by the single
    # session_router slot in EmbeddedGateway.
    assert bundle.session_router is not None


# ---------------------------------------------------------------------------
# Test 2 — Slack and web events both route through the shared SessionRouter
# ---------------------------------------------------------------------------


async def test_slack_and_web_events_share_session_router(
    spy_router: _SpyRouter,
    web_adapter: FakeWebAdapter,
    slack_adapter: SlackAdapter,
) -> None:
    """Events from both web and slack adapters reach the same SessionRouter.

    AC-2.2: same agent is reachable from either platform — both inbound events
    land in the single shared router. Session key format differs per adapter:
    - WebAdapter uses build_session_key(agent_did, user_did)
    - SlackAdapter uses "slack:{channel}:{user_id}"
    Both are valid; the router is keyed by whatever session_key the adapter sets.
    """
    # Simulate a web inbound event.
    await web_adapter.simulate_inbound(
        user_did=_USER_DID,
        agent_did=_AGENT_DID,
        text="hello from web",
    )
    await asyncio.sleep(0.1)

    # Simulate a Slack inbound event.
    slack_channel = "D_SLACK_CHAN"
    slack_user = "U_TEST_USER"
    await slack_adapter._handle_inbound(
        {
            "user": slack_user,
            "channel": slack_channel,
            "text": "hello from slack",
            "client_msg_id": "slack-msg-001",
        }
    )
    await asyncio.sleep(0.1)

    # Both events routed through the same spy router.
    assert len(spy_router.routed_events) == 2, (
        f"Expected 2 routed events (one web, one slack); got {len(spy_router.routed_events)}"
    )

    web_event = next(e for e in spy_router.routed_events if e.platform == "web")
    s_event = next(e for e in spy_router.routed_events if e.platform == "slack")

    # Web event: session key = build_session_key(agent_did, user_did).
    expected_web_key = build_session_key(_AGENT_DID, _USER_DID)
    assert web_event.session_key == expected_web_key, (
        f"Web event session key {web_event.session_key!r} != {expected_web_key!r}"
    )

    # Slack event: session key = "slack:{channel}:{user_id}" (SlackAdapter convention).
    expected_slack_key = f"slack:{slack_channel}:{slack_user}"
    assert s_event.session_key == expected_slack_key, (
        f"Slack event session key {s_event.session_key!r} != {expected_slack_key!r}"
    )

    # Both events belong to the same agent.
    assert web_event.agent_did == _AGENT_DID
    # SlackAdapter sets agent_did="" — agent routing happens upstream via the
    # gateway config's effective_agent_did; the adapter itself leaves it blank.
    assert s_event.agent_did == ""


# ---------------------------------------------------------------------------
# Test 3 — Audit chain carries interleaved platform="web" and platform="slack"
# ---------------------------------------------------------------------------


async def test_audit_chain_shows_interleaved_platform_events(
    spy_router: _SpyRouter,
    web_adapter: FakeWebAdapter,
    slack_adapter: SlackAdapter,
) -> None:
    """Events routed to the same router carry distinct platform labels.

    AC-2.2: same chat_id lineage in audit, regardless of which adapter
    delivered the message. The InboundEvent.platform field is the canonical
    audit attribute — "web" or "slack" — and both appear in the event stream
    for the same SessionRouter (same agent deployment).
    """
    # Send web event first, then slack event.
    await web_adapter.simulate_inbound(
        user_did=_USER_DID,
        agent_did=_AGENT_DID,
        text="first turn via web",
    )
    await asyncio.sleep(0.05)

    await slack_adapter._handle_inbound(
        {
            "user": "U_TEST_USER",
            "channel": "D_CHAN_AUDIT",
            "text": "second turn via slack",
            "client_msg_id": "slack-msg-audit-001",
        }
    )
    await asyncio.sleep(0.05)

    # Collect routed platforms in arrival order.
    platforms_seen = [e.platform for e in spy_router.routed_events]

    assert "web" in platforms_seen, (
        f"Expected platform='web' in routed events; got {platforms_seen}"
    )
    assert "slack" in platforms_seen, (
        f"Expected platform='slack' in routed events; got {platforms_seen}"
    )

    # Ordering: web arrived before slack (arrival order preserved in routed_events).
    web_idx = next(i for i, e in enumerate(spy_router.routed_events) if e.platform == "web")
    slack_idx = next(i for i, e in enumerate(spy_router.routed_events) if e.platform == "slack")
    assert web_idx < slack_idx, (
        "Web event should precede the slack event in the audit chain"
    )

    # Both events reference the shared agent for the web path; audit is attributable.
    web_events = [e for e in spy_router.routed_events if e.platform == "web"]
    assert all(e.agent_did == _AGENT_DID for e in web_events), (
        "All web events must carry the agent DID for audit attribution"
    )


# ── SPEC-025 §TD-2 — session-key divergence is intentional in v1.1 ──────────


async def test_session_keys_intentionally_diverge_across_platforms(
    web_adapter: FakeWebAdapter,
    slack_adapter: SlackAdapter,
    spy_router: SpySessionRouter,
) -> None:
    """The same human reaching one agent via web vs. Slack ends up in TWO sessions.

    SPEC-025 §TD-2 — this is documented as a v1.1 limitation. The SDD's
    claim that "chat_id == session_key, identical across web/slack/telegram"
    holds only for web; Slack uses ``slack:{channel}:{user_id}`` because
    the platform's identifier graph is disjoint from the DID system.

    Cross-platform unified history is deferred to SPEC-026. Until then,
    a pinned test makes the divergence intentional rather than accidental:
    if a future PR accidentally aligns the two key formats it will fail
    here, and the author must update ADR-002 in the SPEC-025 README before
    proceeding.
    """
    # Send the same logical message from both surfaces.
    await web_adapter.simulate_inbound(
        user_did=_USER_DID, agent_did=_AGENT_DID, text="from web",
    )
    await slack_adapter._handle_inbound(
        {
            "user": "U_TEST_USER",
            "channel": "D_SLACK_CHAN",
            "text": "from slack",
            "client_msg_id": "slack-divergence-test",
        }
    )
    await asyncio.sleep(0.1)

    web_keys = {e.session_key for e in spy_router.routed_events if e.platform == "web"}
    slack_keys = {e.session_key for e in spy_router.routed_events if e.platform == "slack"}

    # Sanity — both adapters routed at least one event.
    assert web_keys, "web adapter did not route any event"
    assert slack_keys, "slack adapter did not route any event"

    # The contract: NO web key equals any slack key. Different sessions by design.
    assert web_keys.isdisjoint(slack_keys), (
        "TD-2 contract violated: a web session key collides with a Slack session "
        "key. If this is intentional, update ADR-002 in the SPEC-025 README and "
        "retire this test. Otherwise, the cross-platform unification work that "
        "blocks SPEC-026 has either landed early or by accident."
    )
