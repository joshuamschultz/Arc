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
    UNTRUSTED_INPUT,
    SessionCapabilityLedger,
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
