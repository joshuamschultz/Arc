"""A real deployment with only the network faked — the fixtures every journey shares.

These suites answer one question the rest of the repo does not: *does the thing a
user does actually work?* Not "does the route return 200", not "does the executor
route a frame" — does a person typing a message get an answer back.

Nothing here is a stand-in except the LLM wire. The Arc home, the operator key,
the agent identity, the config load, ``ArcAgent.startup``, the policy pipeline,
the audit chain, the gateway session router, and the arcui websocket route are
all the production objects on the production path. That is the whole point: the
defects these catch — a missing operator key, an unresolvable path, an unwired
module — live in exactly the wiring a mocked agent skips over.

The existing end-to-end chat test was gated on ``ARC_E2E=1`` *and* on a
``team/concierge_agent`` directory that does not exist in a checkout, so it had
never run once. A test that cannot run is not coverage. These run by default.
"""

from __future__ import annotations

import json
import socket
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

#: What the scripted model says when a journey does not care about the words.
DEFAULT_REPLY = "Hello from the scripted model."

VIEWER_TOKEN = "viewer-token-journey"
OPERATOR_TOKEN = "operator-token-journey"


# ---------------------------------------------------------------------------
# The one fake: the LLM wire
# ---------------------------------------------------------------------------


@dataclass
class ScriptedTurn:
    """One model turn: plain text, or a tool call the real dispatcher will run."""

    text: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScriptedLLM:
    """An ``LLMProvider`` that returns queued replies and records what it was asked.

    Implements the provider contract directly rather than patching ``run_stream``
    or ``ArcAgent.run``: everything above the wire — arcrun's loop, tool
    dispatch, the policy pipeline, the context manager, the session write — then
    runs for real. A journey that faked the agent would have passed on every
    deployment this week while every real message failed.

    ``replies`` is consumed in order and accepts a plain string or a
    :class:`ScriptedTurn`; once empty, :data:`DEFAULT_REPLY` is returned, so a
    journey scripts only the turns it makes assertions about.
    """

    replies: list[str | ScriptedTurn] = field(default_factory=list)
    #: Every ``invoke`` call's messages, in order — what the agent actually sent.
    calls: list[list[Any]] = field(default_factory=list)
    #: Which strategy the scripted model picks when the loop asks.
    strategy: str = "react"
    strategy_selections: int = 0

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def model_name(self) -> str:
        return "scripted/model"

    def validate_config(self) -> bool:
        return True

    def _next_turn(self) -> ScriptedTurn:
        if not self.replies:
            return ScriptedTurn(text=DEFAULT_REPLY)
        nxt = self.replies.pop(0)
        return ScriptedTurn(text=nxt) if isinstance(nxt, str) else nxt

    async def invoke(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        from arcllm.types import LLMResponse, ToolCall, Usage

        self.calls.append(list(messages))
        if _is_strategy_selection(tools):
            # Every run now opens by asking which strategy fits the task. A
            # journey is about what the user gets, so the selection is answered
            # here rather than consuming a queued reply that the journey wrote
            # for the turn it actually cares about.
            self.strategy_selections += 1
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id=f"select-{self.strategy_selections}",
                        name="select_strategy",
                        arguments={"strategy": self.strategy},
                    )
                ],
                stop_reason="tool_use",
                usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            )
        turn = self._next_turn()
        tool_calls = (
            [ToolCall(id=f"call-{len(self.calls)}", name=turn.tool, arguments=turn.args)]
            if turn.tool
            else []
        )
        return LLMResponse(
            content=turn.text,
            model=self.model_name,
            provider=self.name,
            stop_reason="tool_use" if tool_calls else "end_turn",
            tool_calls=tool_calls,
            usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        )

    async def invoke_stream(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        from arcllm.types import Delta, ToolCallDelta

        response = await self.invoke(messages, tools, **kwargs)
        # One frame per tool call, then the closing frame — the shape a real
        # streaming adapter emits, so arcrun's accumulator is exercised rather
        # than bypassed by a single fat delta.
        for index, call in enumerate(response.tool_calls):
            yield Delta(
                tool_call=ToolCallDelta(
                    index=index,
                    id=call.id,
                    name=call.name,
                    arguments=json.dumps(call.arguments),
                )
            )
        yield Delta(
            text=response.content,
            usage=response.usage,
            stop_reason=response.stop_reason,
        )

    async def close(self) -> None:
        return None

    @property
    def last_prompt_text(self) -> str:
        """Every message of the most recent call, flattened — for substring asserts."""
        if not self.calls:
            return ""
        return "\n".join(str(getattr(m, "content", m)) for m in self.calls[-1])


@pytest.fixture
def scripted_llm(monkeypatch: pytest.MonkeyPatch) -> ScriptedLLM:
    """Replace the provider factory so no journey can reach a real network.

    Patched at ``arcllm.load_model`` — the lowest seam above the wire — so
    ``arcrun.load_model`` and everything that calls it stay real.
    """
    import arcllm

    model = ScriptedLLM()
    monkeypatch.setattr(arcllm, "load_model", lambda *a, **k: model)
    return model


# ---------------------------------------------------------------------------
# The real deployment
# ---------------------------------------------------------------------------


def free_port() -> int:
    """A port nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def agent_toml(
    agent_dir: Path,
    *,
    name: str,
    modules: tuple[str, ...] = (),
    module_config: dict[str, dict[str, Any]] | None = None,
) -> str:
    """A minimal agent config — the shape ``arc agent create`` writes.

    ``operator_key_dir`` is deliberately left unset so the agent resolves the
    deployment key through ``arctrust.paths``. A config that spells the path out
    is what silently pointed a whole live fleet at an empty directory.
    """
    body = (
        "[agent]\n"
        f"name = '{name}'\n"
        "org = 'journeys'\n"
        "type = 'executor'\n"
        f"workspace = '{agent_dir / 'workspace'}'\n\n"
        "[identity]\n"
        'did = ""\n'
        f"key_dir = '{agent_dir / 'keys'}'\n\n"
        "[telemetry]\n"
        "enabled = false\n\n"
        "[security]\n"
        "tier = 'personal'\n"
    )
    for module in modules:
        body += f"\n[modules.{module}]\nenabled = true\n"
        for key, value in (module_config or {}).get(module, {}).items():
            if key == "__config__":
                body += f"\n[modules.{module}.config]\n"
                body += "".join(f"{k} = {json.dumps(v)}\n" for k, v in value.items())
    return body


@dataclass(frozen=True)
class Deployment:
    """One isolated Arc deployment: its home, its fleet, and one agent in it."""

    home: Path
    team_root: Path
    agent_dir: Path
    agent_name: str


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Deployment]:
    """A complete, isolated deployment with a real operator key and one agent.

    Every path is redirected under ``tmp_path``: a journey that reached the
    developer's real ``~/.arc`` would append to their live operator-signed WORM
    chain, which enforces a single writer and would then fail beside any
    concurrent run.
    """
    home = tmp_path / "arc-home"
    home.mkdir(parents=True)
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("ARC_CONFIG_DIR", str(home))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(store))
    # A port nothing is bound to: messaging must degrade, never hang on a broker.
    monkeypatch.setenv("ARCTEAM_NATS_URL", f"nats://127.0.0.1:{free_port()}")

    from arccli.commands.operator import ensure_operator_key

    ensure_operator_key(home)

    name = "journey"
    agent_dir = tmp_path / "team" / f"{name}_agent"
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(agent_toml(agent_dir, name=name), encoding="utf-8")
    (agent_dir / "arcllm.toml").write_text("[llm]\nmodel = 'scripted/model'\n", encoding="utf-8")

    # Mint the DID and persist it into arcagent.toml — the step `arc agent create`
    # performs for a real user. Without it the config's did is empty, the roster
    # publishes an agent with no DID, and the chat route answers "Agent not
    # found" and closes: a fixture that skipped this would be testing a state no
    # real deployment is ever in.
    from arccli.commands.agent.create import _mint_agent_identity

    _mint_agent_identity(agent_dir)

    yield Deployment(
        home=home,
        team_root=tmp_path / "team",
        agent_dir=agent_dir,
        agent_name=name,
    )


@pytest.fixture
def enable_modules(deployment: Deployment) -> Any:
    """Rewrite the agent config with modules enabled, before the agent is built."""

    def _enable(*modules: str, config: dict[str, dict[str, Any]] | None = None) -> None:
        """Enable ``modules``; ``config`` maps a module name to its ``[…​.config]`` table."""
        module_config = {name: {"__config__": table} for name, table in (config or {}).items()}
        (deployment.agent_dir / "arcagent.toml").write_text(
            agent_toml(
                deployment.agent_dir,
                name=deployment.agent_name,
                modules=modules,
                module_config=module_config,
            ),
            encoding="utf-8",
        )

    return _enable


@pytest.fixture
async def agent(deployment: Deployment, scripted_llm: ScriptedLLM) -> AsyncIterator[Any]:
    """A started ``ArcAgent`` — the real object, built the way the gateway builds it."""
    import arcagent

    config_path = deployment.agent_dir / "arcagent.toml"
    arc_agent = arcagent.ArcAgent(arcagent.load_config(config_path), config_path=config_path)
    await arc_agent.startup()
    try:
        yield arc_agent
    finally:
        await arc_agent.shutdown()


def _is_strategy_selection(tools: list[Any] | None) -> bool:
    """True when this call is the loop asking which strategy to run."""
    return bool(tools) and any(getattr(t, "name", "") == "select_strategy" for t in tools or [])
