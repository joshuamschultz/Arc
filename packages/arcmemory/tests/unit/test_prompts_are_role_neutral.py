"""Memory's own prompts must not claim a role only some agents have.

Four stock prompts opened with "You are the memory of an executive assistant" —
every agent's, regardless of what that agent is. A trader's nightly consolidation
therefore ran believing it was an assistant's memory, which is not merely cosmetic:
these prompts decide what is WORTH recording, and an assistant's durable knowledge
(meetings, follow-ups, contacts) is not a trader's (positions, setups, risk rules).

It also cost real diagnostic time. An operator seeing sales content in a run headed
"the memory of an executive assistant" reasonably read it as the wrong agent's data
— the fleet's sales agent is the one whose identity says Chief of Staff, and the
trader's says Trader, so the line pointed away from whichever agent actually ran.

The agent's own role reaches these prompts through its identity and the episodes
themselves. Naming one here can only ever be wrong for every other agent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_CONTEXT = Path(__file__).resolve().parents[2] / "src" / "arcmemory" / "context"

#: Job titles a shipped prompt must not assert on an agent's behalf.
_ROLES = ("executive assistant", "chief of staff", "sales rep", "trader", "analyst")

_PROMPTS = sorted(_CONTEXT.glob("*.md"))


def test_there_are_prompts_to_check() -> None:
    """Guard the guard — an empty glob would make the check below vacuous."""
    assert len(_PROMPTS) >= 4, f"expected stock prompts in {_CONTEXT}"


@pytest.mark.parametrize("prompt", _PROMPTS, ids=lambda p: p.stem)
def test_a_stock_prompt_claims_no_particular_role(prompt: Path) -> None:
    """One case per prompt, so a failure names the file to fix."""
    text = prompt.read_text(encoding="utf-8").lower()
    found = [role for role in _ROLES if role in text]
    assert not found, (
        f"{prompt.name} tells every agent it is a {found[0]!r}. It reaches the trader, "
        "the coder and the marketer too, and it decides what they consider worth "
        "remembering."
    )
