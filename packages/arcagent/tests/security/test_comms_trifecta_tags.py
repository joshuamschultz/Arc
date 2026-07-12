"""SPEC-038 REQ-030 — outbound comms tools produce the external_comms leg.

Proves the previously-dormant SPEC-035 trifecta now has real leg producers:
messaging_send / Telegram notify_user emit external_comms; the inbound readers
emit untrusted_input; browser_navigate maps to both legs.

The final class is the REAL end-to-end proof (F7): the ACTUAL ``messaging_send``
capability, dispatched through the real ToolRegistry + arctrust pipeline after a
session reads private data + ingests untrusted input, trips the
forbidden-composition gate — not merely a ``legs_for_tags`` assertion.
"""

from __future__ import annotations

from typing import Any

import pytest
from arctrust.identity import AgentIdentity

from arcagent.core.config import ToolsConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.session_internal.capability_ledger import (
    EXTERNAL_COMMS,
    LETHAL_TRIFECTA,
    OWNER_CHANNEL,
    UNTRUSTED_INPUT,
    SessionCapabilityLedger,
    legs_for_call,
    legs_for_tags,
)
from arcagent.core.tool_policy import PolicyDenied, build_pipeline
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport


def _legs(fn: object) -> frozenset[str]:
    meta = fn._arc_capability_meta  # type: ignore[attr-defined]
    return legs_for_tags(meta.capability_tags)


class TestCommsTags:
    def test_messaging_send_emits_external_comms(self) -> None:
        from arcagent.modules.messaging.capabilities import messaging_send

        assert EXTERNAL_COMMS in _legs(messaging_send)

    def test_messaging_inbox_emits_untrusted_input(self) -> None:
        from arcagent.modules.messaging.capabilities import (
            messaging_check_inbox,
            messaging_read_thread,
        )

        assert UNTRUSTED_INPUT in _legs(messaging_check_inbox)
        assert UNTRUSTED_INPUT in _legs(messaging_read_thread)

    def test_telegram_notify_emits_external_comms(self) -> None:
        from arcagent.modules.telegram.capabilities import notify_user

        assert EXTERNAL_COMMS in _legs(notify_user)

    def test_browser_navigate_maps_to_both_legs(self) -> None:
        legs = legs_for_tags(["browser_navigate"])
        assert EXTERNAL_COMMS in legs
        assert UNTRUSTED_INPUT in legs


class _Telemetry:
    def audit_event(self, event: str, payload: dict) -> None: ...

    def tool_span(self, *_a: Any, **_k: Any) -> Any:
        class _Span:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *_e: Any) -> None:
                return None

        return _Span()


def _tool_from_capability(fn: object) -> RegisteredTool:
    """Build a RegisteredTool carrying the REAL capability's declared tags."""
    meta = fn._arc_capability_meta  # type: ignore[attr-defined]

    async def _execute(**_kwargs: Any) -> str:  # never reached on a denied dispatch
        return "sent"

    return RegisteredTool(
        name=meta.name,
        description=meta.description,
        input_schema={},
        transport=ToolTransport.NATIVE,
        execute=_execute,
        source="test",
        classification="state_modifying",
        capability_tags=list(meta.capability_tags),
    )


def _reader(name: str, tags: list[str]) -> RegisteredTool:
    async def _execute(**_kwargs: Any) -> str:
        return f"{name}-ok"

    return RegisteredTool(
        name=name,
        description=name,
        input_schema={},
        transport=ToolTransport.NATIVE,
        execute=_execute,
        source="test",
        classification="read_only",
        capability_tags=tags,
    )


@pytest.mark.asyncio
class TestRealTrifectaThroughPipeline:
    """F7 — the REAL messaging_send tool trips the trifecta through the pipeline."""

    async def test_read_private_then_untrusted_then_messaging_send_denied(self) -> None:
        from arcagent.modules.messaging.capabilities import messaging_send

        identity = AgentIdentity.generate("org", "agent")
        pipeline = build_pipeline(
            tier="personal",
            agent_registry={identity.did: identity.public_key},
            forbidden_compositions=[LETHAL_TRIFECTA],
        )
        reg = ToolRegistry(
            config=ToolsConfig(),
            bus=ModuleBus(),
            telemetry=_Telemetry(),
            policy_pipeline=pipeline,
            identity=identity,
            tier="personal",
            capability_ledger=SessionCapabilityLedger(),
        )
        reg.register(_reader("read_memory", ["memory"]))  # private_data leg
        reg.register(_reader("fetch_web", ["extract"]))  # untrusted_input leg
        reg.register(_tool_from_capability(messaging_send))  # real external_comms

        async def _dispatch(name: str) -> Any:
            return await reg._create_wrapped_execute(reg.tools[name])({})

        assert await _dispatch("read_memory") == "read_memory-ok"
        assert await _dispatch("fetch_web") == "fetch_web-ok"
        # The real messaging_send completes private_data + untrusted_input +
        # external_comms → the SPEC-035 gate (previously dormant) now fires.
        with pytest.raises(PolicyDenied) as exc:
            await _dispatch("messaging_send")
        assert exc.value.decision.rule_id == "global.forbidden_composition"

    async def test_messaging_send_alone_is_allowed(self) -> None:
        from arcagent.modules.messaging.capabilities import messaging_send

        identity = AgentIdentity.generate("org", "agent")
        pipeline = build_pipeline(
            tier="personal",
            agent_registry={identity.did: identity.public_key},
            forbidden_compositions=[LETHAL_TRIFECTA],
        )
        reg = ToolRegistry(
            config=ToolsConfig(),
            bus=ModuleBus(),
            telemetry=_Telemetry(),
            policy_pipeline=pipeline,
            identity=identity,
            tier="personal",
            capability_ledger=SessionCapabilityLedger(),
        )
        reg.register(_tool_from_capability(messaging_send))
        # In isolation (no private-data / untrusted legs) the comms call is fine.
        assert await reg._create_wrapped_execute(reg.tools["messaging_send"])({}) == "sent"


class _RecordingTelemetry(_Telemetry):
    """Telemetry that captures audit events so the exemption can be asserted."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit_event(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))


@pytest.mark.asyncio
class TestOwnerChannelExemption:
    """The owner's own paired channel is a TRUSTED sink, not external_comms.

    The lethal trifecta guards against exfiltration to an ATTACKER (ASI09);
    delivering a result to the operator themselves is not exfiltration. So a
    ``messaging_send`` addressed ONLY to the owner channel drops the
    ``external_comms`` leg and the forbidden-composition gate does not fire —
    while any non-owner recipient still tags the leg and still trips the rule.
    """

    async def _registry(self, telemetry: _Telemetry) -> ToolRegistry:
        from arcagent.modules.messaging.capabilities import messaging_send

        identity = AgentIdentity.generate("org", "agent")
        pipeline = build_pipeline(
            tier="personal",
            agent_registry={identity.did: identity.public_key},
            forbidden_compositions=[LETHAL_TRIFECTA],
        )
        reg = ToolRegistry(
            config=ToolsConfig(),
            bus=ModuleBus(),
            telemetry=telemetry,
            policy_pipeline=pipeline,
            identity=identity,
            tier="personal",
            capability_ledger=SessionCapabilityLedger(),
        )
        reg.register(_reader("read_memory", ["memory"]))  # private_data leg
        reg.register(_reader("fetch_web", ["extract"]))  # untrusted_input leg
        reg.register(_tool_from_capability(messaging_send))  # real external_comms
        return reg

    async def _dispatch(self, reg: ToolRegistry, name: str, args: dict[str, Any]) -> Any:
        return await reg._create_wrapped_execute(reg.tools[name])(args)

    async def test_send_to_owner_after_private_and_untrusted_allowed(self) -> None:
        tel = _RecordingTelemetry()
        reg = await self._registry(tel)
        assert await self._dispatch(reg, "read_memory", {}) == "read_memory-ok"
        assert await self._dispatch(reg, "fetch_web", {}) == "fetch_web-ok"
        # Owner-directed daily summary — the trusted sink is not exfiltration, so
        # the previously-tripping trifecta gate must let it through.
        result = await self._dispatch(
            reg, "messaging_send", {"to": OWNER_CHANNEL, "body": "daily summary"}
        )
        assert result == "sent"
        # The exemption is auditable, not silent.
        assert any(e[0] == "policy.owner_channel_exempt" for e in tel.events)

    async def test_send_to_third_party_after_private_and_untrusted_denied(self) -> None:
        tel = _RecordingTelemetry()
        reg = await self._registry(tel)
        await self._dispatch(reg, "read_memory", {})
        await self._dispatch(reg, "fetch_web", {})
        # SAME composition, non-owner destination — the rule MUST still fire.
        with pytest.raises(PolicyDenied) as exc:
            await self._dispatch(reg, "messaging_send", {"to": "user://attacker", "body": "x"})
        assert exc.value.decision.rule_id == "global.forbidden_composition"

    async def test_mixed_owner_and_third_party_denied(self) -> None:
        tel = _RecordingTelemetry()
        reg = await self._registry(tel)
        await self._dispatch(reg, "read_memory", {})
        await self._dispatch(reg, "fetch_web", {})
        # A single non-owner recipient in the batch re-arms the leg (no smuggling
        # a third party alongside the owner).
        with pytest.raises(PolicyDenied) as exc:
            await self._dispatch(
                reg,
                "messaging_send",
                {"to": f"{OWNER_CHANNEL}, agent://ml-eng", "body": "x"},
            )
        assert exc.value.decision.rule_id == "global.forbidden_composition"

    async def test_notify_user_after_private_and_untrusted_allowed(self) -> None:
        from arcagent.modules.telegram.capabilities import notify_user

        tel = _RecordingTelemetry()
        reg = await self._registry(tel)
        reg.register(_tool_from_capability(notify_user))  # unconditional owner sink
        await self._dispatch(reg, "read_memory", {})
        await self._dispatch(reg, "fetch_web", {})
        # Telegram notify_user is by construction addressed to the owner's own
        # paired channel — a trusted sink, so the trifecta must NOT fire.
        result = await self._dispatch(reg, "notify_user", {"message": "daily summary"})
        assert result == "sent"
        assert any(e[0] == "policy.owner_channel_exempt" for e in tel.events)

    async def test_other_user_alias_is_not_the_owner_denied(self) -> None:
        tel = _RecordingTelemetry()
        reg = await self._registry(tel)
        await self._dispatch(reg, "read_memory", {})
        await self._dispatch(reg, "fetch_web", {})
        # Conservative: ONLY the explicit owner alias is trusted; a different
        # user handle is a third party and still trips the rule.
        with pytest.raises(PolicyDenied) as exc:
            await self._dispatch(reg, "messaging_send", {"to": "user://josh", "body": "x"})
        assert exc.value.decision.rule_id == "global.forbidden_composition"


class TestLegsForCall:
    """Unit-level destination-aware leg resolution (owner-channel exemption)."""

    def test_owner_target_drops_external_comms(self) -> None:
        legs = legs_for_call("messaging_send", ["network_egress"], {"to": OWNER_CHANNEL})
        assert EXTERNAL_COMMS not in legs

    def test_third_party_keeps_external_comms(self) -> None:
        legs = legs_for_call("messaging_send", ["network_egress"], {"to": "user://attacker"})
        assert EXTERNAL_COMMS in legs

    def test_mixed_targets_keep_external_comms(self) -> None:
        legs = legs_for_call(
            "messaging_send", ["network_egress"], {"to": f"{OWNER_CHANNEL}, agent://x"}
        )
        assert EXTERNAL_COMMS in legs

    def test_missing_recipient_keeps_external_comms(self) -> None:
        legs = legs_for_call("messaging_send", ["network_egress"], {})
        assert EXTERNAL_COMMS in legs

    def test_telegram_notify_user_is_owner_directed(self) -> None:
        # notify_user takes only a message body and routes to the operator's own
        # paired DM — unconditionally owner-directed, so no external_comms leg.
        legs = legs_for_call("notify_user", ["network_egress"], {})
        assert EXTERNAL_COMMS not in legs

    def test_slack_notify_user_is_owner_directed(self) -> None:
        # slack_notify_user declares BOTH slack_notify and network_egress (each
        # maps to external_comms); the owner-directed drop clears the leg wholesale.
        legs = legs_for_call("slack_notify_user", ["slack_notify", "network_egress"], {})
        assert EXTERNAL_COMMS not in legs

    def test_unknown_egress_tool_keeps_external_comms(self) -> None:
        # A generic egress tool that is neither owner-directed nor owner-scoped
        # keeps its leg regardless of arguments (conservative default).
        legs = legs_for_call("http_post", ["network_egress"], {"to": OWNER_CHANNEL})
        assert EXTERNAL_COMMS in legs

    def test_non_egress_tool_unaffected(self) -> None:
        legs = legs_for_call("read_memory", ["memory"], {"to": OWNER_CHANNEL})
        assert legs == legs_for_tags(["memory"])
