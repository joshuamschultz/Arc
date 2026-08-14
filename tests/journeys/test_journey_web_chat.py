"""Journey: a person types a message in the dashboard and gets an answer.

This is the single most-used path in Arc and, until now, nothing exercised it
end to end. The consequence was not theoretical: a deployment shipped whose
service was active, whose ``/health`` returned 200, whose logs held zero
tracebacks, whose preflight passed every check — and whose every message
answered ``[agent-error] the run failed``, because the agent could not load the
operator key it signs its audit chain with. Every green signal was measuring
something other than the thing the user does.

The chat pipe below is entirely real: arcui route → web adapter → SessionRouter
→ AsyncioExecutor → the gateway's own agent factory → ``ArcAgent.startup`` →
arcrun's loop. Only the LLM wire is scripted.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from arcgateway.config import GatewayConfig
from starlette.testclient import TestClient

from .conftest import OPERATOR_TOKEN, VIEWER_TOKEN, Deployment, ScriptedLLM, ScriptedTurn

#: Total seconds to wait for a turn to produce its reply. One deadline for the
#: whole read, not one per frame: a turn emits many status/token frames first,
#: and a per-frame timeout multiplies by however many arrive.
_TURN_DEADLINE = 30.0


@pytest.fixture
def client(deployment: Deployment, scripted_llm: ScriptedLLM) -> Iterator[TestClient]:
    """An arcui app serving the real fleet, wired the way ``arc ui start`` wires it."""
    from arcui.auth import AuthConfig
    from arcui.server import create_app

    app = create_app(
        team_root=deployment.team_root,
        auth_config=AuthConfig({"viewer_token": VIEWER_TOKEN, "operator_token": OPERATOR_TOKEN}),
        gateway_config=GatewayConfig.from_toml_str("[platforms.web]\nenabled = true\n"),
    )
    with TestClient(app) as test_client:
        yield test_client


def _receive(ws: Any, timeout: float = 10.0) -> Any:
    """Read one frame, or fail — never block the suite forever.

    ``receive_json`` waits with no deadline, so a pipe that dispatches nothing
    hangs the whole run instead of reporting it. That is a large part of why a
    broken chat path stayed invisible: the one test that drove it could only
    hang or be skipped.

    The wait is cancelled INSIDE the test client's own event loop. Timing out a
    reader thread from outside instead leaves an abandoned call pending on the
    portal, and closing the socket then deadlocks against it — the failure
    never prints and the suite wedges, which is strictly worse than the hang it
    was meant to replace.
    """
    import anyio

    async def _next_message() -> Any:
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    try:
        message = ws.portal.call(_next_message)
    except TimeoutError:
        raise AssertionError(
            f"no websocket frame within {timeout}s — nothing was dispatched"
        ) from None
    ws._raise_on_close(message)
    return json.loads(message["text"])


def _open_chat(client: TestClient, agent_id: str) -> Any:
    """Connect, authenticate, and consume the ``ready`` frame."""
    ws = client.websocket_connect(f"/ws/chat/{agent_id}").__enter__()
    ws.send_json({"token": VIEWER_TOKEN})
    ready = _receive(ws)
    assert ready["type"] == "ready", f"expected ready, got {ready}"
    return ws


def _reply_text(ws: Any) -> str:
    """Read frames until the agent's message arrives; fail loudly with what came.

    An ``agent-error`` frame is reported as the failure rather than silently
    consumed — it is precisely the frame the live deployment was returning while
    every other signal read healthy.
    """
    seen: list[Any] = []
    deadline = time.monotonic() + _TURN_DEADLINE
    while time.monotonic() < deadline:
        frame = _receive(ws, timeout=max(1.0, deadline - time.monotonic()))
        seen.append(frame)
        text = str(frame.get("text", ""))
        if "[agent-error]" in text:
            raise AssertionError(f"the agent failed the turn: {text}")
        if frame.get("type") == "message" and frame.get("from") == "agent":
            return text
    raise AssertionError(f"no agent message within {_TURN_DEADLINE}s; frames seen: {seen}")


def test_a_user_message_gets_an_agent_reply(client: TestClient, scripted_llm: ScriptedLLM) -> None:
    """The whole point: type something, get an answer back."""
    scripted_llm.replies.append("The quarterly numbers are up.")

    ws = _open_chat(client, "journey")
    try:
        ws.send_json({"type": "message", "text": "How did the quarter go?"})
        assert _reply_text(ws) == "The quarterly numbers are up."
    finally:
        ws.__exit__(None, None, None)

    assert scripted_llm.calls, "the agent answered without ever calling the model"
    assert "How did the quarter go?" in scripted_llm.last_prompt_text


def test_a_tool_call_runs_and_is_signed_into_the_audit_chain(
    client: TestClient, scripted_llm: ScriptedLLM
) -> None:
    """The agent calls a real tool, the result comes back, and the call is signed.

    Answering is only half of what Arc claims. Every tool dispatch is supposed to
    pass the policy pipeline and land in the operator-signed WORM chain, so a
    deployment that chats happily while recording nothing verifiable is a failure
    of the product, not a missing nice-to-have. Both halves are asserted here
    because a turn can very easily do the first and skip the second.
    """
    from arcstore import resolve_data_dir
    from arctrust import OperatorKey
    from arctrust.audit import verify_chain
    from arctrust.paths import default_operator_key_path

    scripted_llm.replies.extend(
        [ScriptedTurn(tool="ls", args={"path": "."}), "I listed the workspace."]
    )

    ws = _open_chat(client, "journey")
    try:
        ws.send_json({"type": "message", "text": "list the files"})
        assert _reply_text(ws) == "I listed the workspace."
    finally:
        ws.__exit__(None, None, None)

    assert len(scripted_llm.calls) >= 2, "the tool result was never fed back to the model"

    chains = [c for c in (resolve_data_dir(None) / "worm").glob("*.jsonl") if c.stat().st_size]
    assert chains, "the tool call was dispatched but nothing was written to the audit chain"

    public_key = OperatorKey.load(default_operator_key_path(), generate_if_absent=False).public_key
    for chain in chains:
        assert verify_chain(chain, public_key), f"{chain.name} does not verify"


def test_a_missing_operator_key_fails_the_turn_loudly(
    client: TestClient, deployment: Deployment
) -> None:
    """Delete the audit authority and the turn must fail — never answer regardless.

    This is the exact production failure, reproduced. It is pinned in both
    directions: the reply path above proves a healthy deployment answers, and
    this proves a deployment that cannot sign refuses to. Without the second, a
    change that quietly dropped audit signing would leave every other test green.
    """
    from arctrust.paths import default_operator_key_path

    default_operator_key_path().unlink()

    ws = _open_chat(client, "journey")
    try:
        ws.send_json({"type": "message", "text": "hello"})
        with pytest.raises(AssertionError, match=r"agent failed the turn|no agent message"):
            _reply_text(ws)
    finally:
        ws.__exit__(None, None, None)


def test_the_reply_is_persisted_to_the_session(
    client: TestClient, deployment: Deployment, scripted_llm: ScriptedLLM
) -> None:
    """A user who reloads the dashboard must still see what was said.

    The websocket frame arriving is not the same claim as the turn being stored;
    session replay reads from disk, so a turn that streams and never persists
    looks perfect live and is gone on refresh.
    """
    scripted_llm.replies.append("Persisted answer.")

    ws = _open_chat(client, "journey")
    try:
        ws.send_json({"type": "message", "text": "remember this"})
        assert _reply_text(ws) == "Persisted answer."
    finally:
        ws.__exit__(None, None, None)

    sessions = deployment.agent_dir / "workspace" / "sessions"
    stored = "\n".join(p.read_text(encoding="utf-8") for p in sessions.rglob("*") if p.is_file())
    assert "remember this" in stored, f"the user's message was never persisted under {sessions}"
    assert "Persisted answer." in stored, "the agent's reply was never persisted"
