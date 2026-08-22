"""Journey: the agent can find and follow the operator's recorded way of doing things.

Procedures are the most operationally valuable thing memory holds — they are how
the operator wants work done — and on a live agent they were unreachable. The
listing and read tools existed only inside the consolidation loop; the running
agent's sole memory tool was ``memory_search``, so a procedure competed as plain
text against 85 entity cards and the agent answered from a skill instead, never
consulting the playbook.

These drive the real agent with real procedure cards on disk. Only the LLM is
scripted, so what is asserted is that the tools exist, reach the store, and record
what a mature memory needs to know: which playbooks are actually reached for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .conftest import Deployment, ScriptedLLM, ScriptedTurn
from .test_journey_modules import _SOURCE_CATALOG, install_modules, start_agent


@pytest.fixture(autouse=True)
def _module_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODULE_SOURCE", str(_SOURCE_CATALOG))


def seed_procedures(deployment: Deployment) -> Path:
    """Write two procedure cards the way consolidation would."""
    cards = deployment.agent_dir / "workspace" / "memory" / "procedures"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / "quote-a-customer.md").write_text(
        "---\ntype: ArcMemoryProcedure\nslug: quote-a-customer\ntitle: Quote a customer\n"
        "when_to_use: A customer asks for pricing on a configured system\n"
        "use_count: 0\nrevisions: 2\nclassification: unclassified\n---\n\n"
        "# Quote a customer\n\n## Steps\n"
        "1. Confirm the configuration in writing before pricing it\n"
        "2. Apply the contract discount, never the list price\n"
        "3. Send the quote with an expiry date\n",
        encoding="utf-8",
    )
    (cards / "onboard-a-logo.md").write_text(
        "---\ntype: ArcMemoryProcedure\nslug: onboard-a-logo\ntitle: Onboard a new logo\n"
        "when_to_use: A new customer signs\nuse_count: 0\nrevisions: 1\n"
        "classification: unclassified\n---\n\n"
        "# Onboard a new logo\n\n## Steps\n1. Create the account record\n",
        encoding="utf-8",
    )
    return cards


async def _with_memory(deployment: Deployment, enable_modules: Any) -> Any:
    enable_modules("memory", config={"memory": {"brain": "arcmemory"}})
    rows = install_modules(deployment)
    assert not [row for row in rows if "REFUSED" in row], f"install refused: {rows}"
    return await start_agent(deployment)


def _tool_result(scripted_llm: ScriptedLLM) -> str:
    """The most recent tool-result content the model was handed."""
    for message in reversed(scripted_llm.calls[-1]):
        if getattr(message, "role", "") == "tool":
            return "\n".join(
                str(getattr(block, "content", "")) for block in (message.content or [])
            )
    return ""


async def _drive(agent: Any, text: str) -> str:
    session = await agent.session("procedures")
    out: list[str] = []
    async for event in agent.run(text, session=session):
        piece = getattr(event, "text", None)
        if isinstance(piece, str):
            out.append(piece)
    return "".join(out)


async def test_the_agent_has_tools_to_reach_its_procedures(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """The gap that made 36 live procedures invisible: no tool to reach them."""
    seed_procedures(deployment)
    agent = await _with_memory(deployment, enable_modules)
    try:
        tools = set(agent._tool_registry.tools)
        assert {"procedure_list", "procedure_get"} <= tools, (
            f"the agent cannot reach its own procedures; it has {sorted(tools)}"
        )
    finally:
        await agent.shutdown()


async def test_listing_shows_triggers_and_withholds_steps(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """The listing must stay affordable, or the agent cannot consult it routinely.

    Withholding steps is the point: a mature store's full playbooks do not fit in a
    turn, so a listing that carried them would simply never be called.
    """
    seed_procedures(deployment)
    agent = await _with_memory(deployment, enable_modules)
    try:
        scripted_llm.replies.extend([ScriptedTurn(tool="procedure_list"), "Here they are."])
        assert "Here they are." in await _drive(agent, "what procedures do we have?")

        # Scoped to the TOOL RESULT: the surrounding prompt also carries whatever
        # automatic recall injected, which is a different channel with its own budget.
        listing = _tool_result(scripted_llm)
        assert "quote-a-customer" in listing
        assert "A customer asks for pricing" in listing, "the trigger is what decides relevance"
        assert "contract discount" not in listing, (
            "the listing spent the context it exists to save"
        )
    finally:
        await agent.shutdown()


async def test_reading_a_procedure_returns_its_steps_and_counts_the_use(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """Following a playbook is the moment usage becomes measurable.

    Both halves are asserted: the steps reach the model (otherwise the agent cannot
    follow them) and the count moves on disk (otherwise nothing ever learns which
    playbooks earn their keep — the live state, where 36 cards showed no uses).
    """
    cards = seed_procedures(deployment)
    agent = await _with_memory(deployment, enable_modules)
    try:
        scripted_llm.replies.extend(
            [
                ScriptedTurn(tool="procedure_get", args={"slug": "quote-a-customer"}),
                "Following it.",
            ]
        )
        assert "Following it." in await _drive(agent, "quote this customer for me")

        seen = "\n".join(str(m) for m in scripted_llm.calls[-1])
        assert "Apply the contract discount" in seen, "the steps never reached the model"
    finally:
        await agent.shutdown()

    assert "use_count: 1" in (cards / "quote-a-customer.md").read_text(encoding="utf-8"), (
        "the procedure was used and the card still records no use"
    )


async def test_using_one_procedure_does_not_touch_the_others(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """Use counts only mean something if they are attributed to what was used."""
    cards = seed_procedures(deployment)
    agent = await _with_memory(deployment, enable_modules)
    try:
        scripted_llm.replies.extend(
            [ScriptedTurn(tool="procedure_get", args={"slug": "quote-a-customer"}), "Done."]
        )
        await _drive(agent, "quote this customer")
    finally:
        await agent.shutdown()

    assert "use_count: 0" in (cards / "onboard-a-logo.md").read_text(encoding="utf-8")
