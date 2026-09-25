"""RoutedAttachment — one tool name, many granted connections, routed not trusted.

Each member here is a recording stand-in for a connection's own attachment, so
every assertion is about WHICH connection ran a call and with WHAT arguments.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from arctrust.audit import AuditEvent

from arcagent.extension.attachment import ProbeResult, ToolOutcome, ToolResult, ToolSpec
from arcagent.extension.manifest import ToolRouting
from arcagent.modules.connectors.routing import RoutedAttachment, RoutedMember

_A = "josh@blackarcindustrial.com"
_B = "josh@blackarcsystems.com"
_REFUSE = r"(?i)(^|[\s=])(--?(account|client|home|access-token)\b|-a(\s|=|$))|\bGOG_[A-Z_]+\s*="


class _Member:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        return ProbeResult(reachable=True)

    async def describe_tools(self) -> list[ToolSpec]:
        return []

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        self.calls.append((tool, args))
        return ToolResult(tool=tool, content=json.dumps({"from": self.name}))


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


_SPECS = [
    ToolSpec(
        name="read",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        classification="read_only",
    ),
    ToolSpec(name="send", classification="state_modifying", capability_tags=["network_egress"]),
]


def _router(
    *members: tuple[str, str, bool],
    granted: set[str] | None = None,
    approved: frozenset[str] = frozenset({"read", "send"}),
) -> tuple[RoutedAttachment, dict[str, _Member], _Sink]:
    fakes = {instance: _Member(instance) for instance, _, _ in members}
    sink = _Sink()
    still = granted if granted is not None else set(fakes)

    async def active(instance: str) -> bool:
        return instance in still

    router = RoutedAttachment(
        extension="google_workspace",
        routing=ToolRouting(
            argument="account",
            field="account",
            match="email",
            refuse_values=_REFUSE,
            free_text=["body"],
        ),
        members=[
            RoutedMember(
                instance=instance,
                attachment=fakes[instance],
                selector=selector,
                approved=approved,
                read_only=read_only,
            )
            for instance, selector, read_only in members
        ],
        specs=_SPECS,
        agent_did="did:arc:agent",
        tier="personal",
        sink=sink,
        grant_active=active,
    )
    return router, fakes, sink


async def test_one_connection_needs_no_selector() -> None:
    router, fakes, _ = _router(("blackarc", _A, False))
    result = await router.invoke("read", {"q": "x"})
    assert result.outcome is ToolOutcome.OK
    assert fakes["blackarc"].calls == [("read", {"q": "x"})]
    assert json.loads(result.content) == {
        "connection": "blackarc",
        "account": _A,
        "result": {"from": "blackarc"},
    }


async def test_several_connections_and_no_selector_fails_closed_listing_them() -> None:
    router, fakes, sink = _router(("blackarc", _A, False), ("systems", _B, False))
    result = await router.invoke("read", {})
    assert result.outcome is ToolOutcome.ERROR
    assert _A in result.content and _B in result.content
    assert not fakes["blackarc"].calls and not fakes["systems"].calls
    assert sink.events[-1].action == "connector.account.denied"


async def test_the_selector_routes_to_its_own_connection_and_is_stripped() -> None:
    router, fakes, sink = _router(("blackarc", _A, False), ("systems", _B, False))
    await router.invoke("read", {"account": _B, "q": "y"})
    assert fakes["systems"].calls == [("read", {"q": "y"})]
    assert fakes["blackarc"].calls == []
    event = sink.events[-1]
    assert (event.action, event.extra["connection"], event.extra["account"]) == (
        "connector.account.routed",
        "systems",
        _B,
    )


@pytest.mark.parametrize(
    "variant",
    ["JOSH@BLACKARCSYSTEMS.COM", "\uff4a\uff4f\uff53\uff48@blackarcsystems.com"],
)
async def test_case_and_width_variants_of_a_held_account_resolve_to_it(variant: str) -> None:
    router, fakes, _ = _router(("blackarc", _A, False), ("systems", _B, False))
    await router.invoke("read", {"account": variant})
    assert fakes["systems"].calls


@pytest.mark.parametrize(
    "variant",
    [
        _B,
        " " + _B,
        _B + "\n",
        _B.replace("o", "\u043e"),  # Cyrillic o
        "systems",  # a connection NAME, not its account
        "work",  # a gog alias
        _B + "\u200b",
        "",
    ],
)
async def test_an_agent_holding_only_a_cannot_reach_b(variant: str) -> None:
    router, fakes, sink = _router(("blackarc", _A, False))
    result = await router.invoke("send", {"account": variant} if variant else {"account": "x"})
    assert result.outcome is ToolOutcome.ERROR
    assert fakes["blackarc"].calls == []
    denied = sink.events[-1]
    assert denied.action == "connector.account.denied"
    assert denied.extra["allowed"] == 1


@pytest.mark.parametrize(
    "smuggled",
    [
        "from:x --account=" + _B,
        "-a " + _B,
        "in:inbox --client=evil",
        "--home=/tmp/x",
        "--access-token=abc",
        "GOG_ACCOUNT=" + _B,
        "x -a=" + _B,
    ],
)
async def test_flags_and_environment_smuggled_in_arguments_are_refused(smuggled: str) -> None:
    router, fakes, _ = _router(("blackarc", _A, False))
    result = await router.invoke("read", {"q": smuggled})
    assert result.outcome is ToolOutcome.ERROR
    assert fakes["blackarc"].calls == []


async def test_free_text_may_say_anything() -> None:
    router, fakes, _ = _router(("blackarc", _A, False))
    await router.invoke("send", {"body": "run gog --account=x please"})
    assert fakes["blackarc"].calls


async def test_a_read_only_connection_refuses_a_write_tool_with_a_sentence() -> None:
    router, fakes, sink = _router(("blackarc", _A, True))
    assert (await router.invoke("read", {})).outcome is ToolOutcome.OK
    result = await router.invoke("send", {})
    assert result.outcome is ToolOutcome.ERROR
    assert "read-only" in result.content
    assert [call[0] for call in fakes["blackarc"].calls] == ["read"]
    assert sink.events[-1].extra["reason"] == "read_only"


async def test_a_revoked_grant_is_refused_at_call_time() -> None:
    router, fakes, _ = _router(("blackarc", _A, False), granted=set())
    result = await router.invoke("read", {})
    assert result.outcome is ToolOutcome.ERROR
    assert fakes["blackarc"].calls == []


async def test_a_tool_not_approved_for_that_connection_is_refused() -> None:
    router, fakes, _ = _router(("blackarc", _A, False), approved=frozenset({"read"}))
    result = await router.invoke("send", {})
    assert "Approve" in result.content
    assert fakes["blackarc"].calls == []


async def test_every_schema_offers_the_optional_selector() -> None:
    router, _, _ = _router(("blackarc", _A, False))
    for spec in await router.describe_tools():
        assert "account" in spec.input_schema["properties"]
        assert "account" not in spec.input_schema.get("required", [])


async def test_an_error_names_the_connection_that_answered() -> None:
    router, fakes, _ = _router(("blackarc", _A, False))

    async def failing(tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content="boom")

    fakes["blackarc"].invoke = failing  # type: ignore[method-assign]
    result = await router.invoke("read", {})
    assert result.content.startswith(f"[connection blackarc, account {_A}]")


@pytest.mark.parametrize("variant", [" " + _A, _A + " ", _A + "\u200b", "\u00a0" + _A])
async def test_even_a_held_account_is_refused_with_whitespace_or_invisibles(variant: str) -> None:
    """Nothing beyond NFKC and case folding is forgiven — not even for the right account."""
    router, fakes, _ = _router(("blackarc", _A, False), ("systems", _B, False))
    result = await router.invoke("read", {"account": variant})
    assert result.outcome is ToolOutcome.ERROR
    assert fakes["blackarc"].calls == []
