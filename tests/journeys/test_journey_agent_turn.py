"""Journey: the agent itself completes a turn — the layer under every surface.

Every channel (dashboard, Telegram, schedule, task) ends in the same call:
``ArcAgent.run`` against a session. If this is broken, every surface is broken,
and a failure here says so in one line instead of through a websocket timeout.

Real: config load, identity, operator key, policy pipeline, session manager,
arcrun's loop. Scripted: the LLM wire only.
"""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import ScriptedLLM


async def _say(arc_agent: Any, text: str, *, key: str = "journey-session") -> str:
    """Drive one full turn and return the assistant's text."""
    session = await arc_agent.session(key)
    chunks: list[str] = []
    async for event in arc_agent.run(text, session=session):
        piece = getattr(event, "text", None) or getattr(event, "content", None)
        if isinstance(piece, str):
            chunks.append(piece)
    return "".join(chunks)


async def test_the_agent_answers_a_message(agent: Any, scripted_llm: ScriptedLLM) -> None:
    """A started agent, given input, produces the model's answer."""
    scripted_llm.replies.append("Two plus two is four.")

    assert "Two plus two is four." in await _say(agent, "What is two plus two?")
    assert "What is two plus two?" in scripted_llm.last_prompt_text


async def test_the_agent_remembers_within_a_session(agent: Any, scripted_llm: ScriptedLLM) -> None:
    """Turn two must carry turn one — otherwise every conversation is amnesiac.

    Asserted on what reached the MODEL, not on the reply: a scripted model would
    happily answer "Ada" whether or not the history was sent, so checking the
    answer proves nothing about context.
    """
    scripted_llm.replies.extend(["Nice to meet you, Ada.", "Your name is Ada."])

    await _say(agent, "My name is Ada.")
    await _say(agent, "What is my name?")

    prompt = scripted_llm.last_prompt_text
    assert "My name is Ada." in prompt, "turn two was sent without turn one's history"
    assert "Nice to meet you, Ada." in prompt, "the assistant's own turn was not carried"


async def test_separate_sessions_do_not_leak_into_each_other(
    agent: Any, scripted_llm: ScriptedLLM
) -> None:
    """Two conversations with one agent must stay separate.

    One agent serves the dashboard, Telegram, and its schedules at once. If
    sessions shared history, a private note in one channel would appear in the
    prompt of another — the cross-context bleed this repo has already had once.
    """
    await _say(agent, "The passphrase is orange-battery.", key="channel-a")
    await _say(agent, "What is the weather?", key="channel-b")

    assert "orange-battery" not in scripted_llm.last_prompt_text, (
        "channel-a's content reached channel-b's prompt"
    )


async def test_a_turn_is_recorded_where_the_dashboard_reads_it(agent: Any) -> None:
    """The turn must land in the session store the replay view reads.

    A turn that streams and never persists looks perfect live and is gone on
    refresh, so the visible behavior and the stored record are separate claims.
    """
    await _say(agent, "persist me")

    session_root = agent._workspace / "sessions"
    stored = "\n".join(
        p.read_text(encoding="utf-8") for p in session_root.rglob("*") if p.is_file()
    )
    assert "persist me" in stored, f"nothing under {session_root} holds the turn"


async def test_startup_refuses_when_the_operator_key_is_gone(
    deployment: Any, scripted_llm: ScriptedLLM
) -> None:
    """No audit authority, no agent — the fail-closed guard that fired in production.

    Pinned as a REQUIREMENT, not an accident: an agent that ran anyway would
    produce turns nothing can verify, which is the AU-9 repudiation the whole
    signing chain exists to prevent.
    """
    import arcagent
    from arctrust.operator import OperatorKeyIntegrityError
    from arctrust.paths import default_operator_key_path

    config_path = deployment.agent_dir / "arcagent.toml"
    arc_agent = arcagent.ArcAgent(arcagent.load_config(config_path), config_path=config_path)
    default_operator_key_path().unlink()

    with pytest.raises(OperatorKeyIntegrityError):
        await arc_agent.startup()
