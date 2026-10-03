"""Operator incident 2026-10-03 — 103 Approve clicks in ~85 minutes for one chat.

Reproduces the observed DGX sequence for ``josh_agent`` (Olivia), web-chat
session ``82dca58cd015a1f1``:

1. ``read``/``grep``/``knowledge_search`` light ``private_data``;
2. ``bash`` lights ``untrusted_input``;
3. ``dropbox_upload`` lights ``external_comms`` -> trifecta complete -> the
   operator clicks Approve once (one-shot grant bound to that call's hash);
4. the agent then fans out ~94 read-only connector calls (``slack_search``,
   ``slack_read_channel``, ``dropbox_list`` ...) that carry NO trifecta leg —
   and EVERY one of them prompted again, because the session union already
   holds all three legs and ``GlobalLayer`` tests the union, not the call;
5. a second ``dropbox_upload`` (different content) prompted again.

The two ``xfail(strict=True)`` tests below state the operator's expectation.
They are red today on purpose; each needs a design ruling before it is made
green (SPEC-035 REQ-015 AC1 currently REQUIRES step 5 to re-prompt, and
SPEC-035 OQ-3 leaves a standing per-session grant undecided). ``strict=True``
means the day a fix lands, the marker must be removed — it cannot silently
keep "expecting failure".

The passing test pins the part of the design that already holds (SPEC-057
G4/REQ-010): delivering to the operator's own channel is not an exfiltration
leg, so chat replies never complete the trifecta.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arctrust.identity import AgentIdentity
from arctrust.policy import ApprovalGrant, OperatorApprovalAuthority, sign_approval_for_hash
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey

from arcagent.core.config import ToolsConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.session_internal.capability_ledger import (
    EXTERNAL_COMMS,
    LETHAL_TRIFECTA,
    OWNER_CHANNEL,
    SessionCapabilityLedger,
    bind_session_id,
    reset_session_id,
)
from arcagent.core.tool_policy import build_pipeline
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport
from arcagent.tools.human_gate import ApprovalRequest, HumanGate

_SESSION = "82dca58cd015a1f1"


class _Telemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit_event(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))

    def tool_span(self, *_a: Any, **_k: Any) -> Any:
        class _Span:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *_e: Any) -> None:
                return None

        return _Span()


def _tool(name: str, tags: list[str]) -> RegisteredTool:
    async def _execute(**kwargs: Any) -> str:
        return f"{name} ok {sorted(kwargs)}"

    return RegisteredTool(
        name=name,
        description=name,
        input_schema={},
        transport=ToolTransport.NATIVE,
        execute=_execute,
        source="test",
        classification="state_modifying",
        capability_tags=tags,
    )


# The tags each tool really carries on the DGX (extensions/slack + dropbox
# manifests; built-in read/bash): connector reads declare NO capability tags.
_TOOLS = [
    _tool("read", ["file_read"]),
    _tool("bash", ["subprocess"]),
    _tool("dropbox_upload", ["network_egress"]),
    _tool("slack_search", []),
    _tool("slack_read_channel", []),
    _tool("notify_user", ["network_egress"]),
    _tool("messaging_send", ["network_egress"]),
]


class _Operator:
    """The arcui Approve button: records each prompt, signs every one."""

    def __init__(self) -> None:
        self.signer = InProcessSigner(bytes(SigningKey.generate()))
        self._authority = OperatorApprovalAuthority(self.signer)
        self.prompts: list[ApprovalRequest] = []

    async def approve(self, request: ApprovalRequest) -> ApprovalGrant | None:
        self.prompts.append(request)
        return sign_approval_for_hash(request.call_hash, self._authority)


def _olivia(operator: _Operator) -> ToolRegistry:
    identity = AgentIdentity.generate("local", "executor")
    gate = HumanGate(
        operator_signer=operator.signer,
        agent_did=identity.did,
        tier="personal",
        channel=operator.approve,
    )
    reg = ToolRegistry(
        config=ToolsConfig(),
        bus=ModuleBus(),
        telemetry=_Telemetry(),
        policy_pipeline=build_pipeline(
            tier="personal",
            agent_registry={identity.did: identity.public_key},
            forbidden_compositions=[LETHAL_TRIFECTA],
        ),
        identity=identity,
        tier="personal",
        capability_ledger=SessionCapabilityLedger(),
        human_gate=gate,
    )
    for tool in _TOOLS:
        reg.register(tool)
    return reg


async def _call(reg: ToolRegistry, name: str, **args: Any) -> Any:
    token = bind_session_id(_SESSION)
    try:
        return await reg._create_wrapped_execute(reg.tools[name])(args)
    finally:
        reset_session_id(token)


async def _complete_trifecta_with_one_approval(reg: ToolRegistry, operator: _Operator) -> None:
    await _call(reg, "read", path="notes/brian.md")
    await _call(reg, "bash", command="ls exports/")
    await _call(reg, "dropbox_upload", path="/Olivia/brian.md", content="v1", mode="add")
    assert len(operator.prompts) == 1, "the completing upload must prompt exactly once"


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "2026-10-03 incident: a call carrying NO trifecta leg is gated once the "
        "session union is saturated (GlobalLayer tests session|call, not the call). "
        "Needs a ruling: does a leg-less call ever need approval? See report."
    ),
)
async def test_legless_reads_after_one_approval_do_not_prompt() -> None:
    operator = _Operator()
    reg = _olivia(operator)
    await _complete_trifecta_with_one_approval(reg, operator)

    for channel in ("C01", "C02", "C03"):
        await _call(reg, "slack_read_channel", channel=channel, limit="50")
    await _call(reg, "slack_search", query="Brian Trentham")

    assert len(operator.prompts) == 1, [p.tool_name for p in operator.prompts]


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "Operator intent #2 (approve a combination once, it stays approved) "
        "contradicts SPEC-035 REQ-015 AC1 (one-shot, call-hash bound) and the "
        "ScenarioGrant design (interactive origin=None matches no standing grant). "
        "Needs a ruling on grant scope. See report."
    ),
)
async def test_same_combination_again_does_not_prompt() -> None:
    operator = _Operator()
    reg = _olivia(operator)
    await _complete_trifecta_with_one_approval(reg, operator)

    await _call(reg, "dropbox_upload", path="/Olivia/clay.md", content="v2", mode="add")

    assert len(operator.prompts) == 1, [p.tool_name for p in operator.prompts]


@pytest.mark.asyncio
async def test_reply_to_owner_channel_is_not_an_exfil_leg() -> None:
    """SPEC-057 G4/REQ-010 — the operator's own channel never completes the trifecta."""
    operator = _Operator()
    reg = _olivia(operator)
    await _call(reg, "read", path="notes/brian.md")
    await _call(reg, "bash", command="curl https://example.com")

    await _call(reg, "notify_user", message="Brian's notes are in Dropbox.")
    await _call(reg, "messaging_send", to=OWNER_CHANNEL, body="done")

    assert operator.prompts == []
    ledger = reg._capability_ledger
    assert ledger is not None
    assert EXTERNAL_COMMS not in ledger.snapshot(_SESSION)


@pytest.mark.asyncio
async def test_non_owner_recipient_still_completes_the_trifecta() -> None:
    """The owner exemption never widens to a third party (abuse case)."""
    operator = _Operator()
    reg = _olivia(operator)
    await _call(reg, "read", path="notes/brian.md")
    await _call(reg, "bash", command="curl https://example.com")

    await _call(reg, "messaging_send", to=f"{OWNER_CHANNEL},agent://stranger", body="x")

    assert len(operator.prompts) == 1


def test_fixture_tools_match_shipped_manifests() -> None:
    """The repro is only honest if connector reads really ship tag-less."""
    root = Path(__file__).resolve().parents[4] / "extensions"
    slack = (root / "slack" / "extension.toml").read_text()
    dropbox = (root / "dropbox" / "extension.toml").read_text()
    for manifest, verb in ((slack, "slack_search"), (slack, "slack_read_channel")):
        block = manifest.split(f'name = "{verb}"', 1)[1].split("[[", 1)[0]
        assert "capability_tags" not in block, verb
    upload = dropbox.split('name = "dropbox_upload"', 1)[1].split("[[", 1)[0]
    assert 'capability_tags = ["network_egress"]' in upload
