"""Prompt templates arcskill ships, resolved through an ``arcprompt.PromptSource``.

arcskill houses the improver prompts it ships (``context/<name>.md``) but never
reads them itself. Every consumer takes a :class:`~arcprompt.PromptSource`
(COMP-030): the agent hands it an overlay-aware, signature-verified source so a
signed operator override authored through ArcUI/CLI takes effect; a component
constructed standalone uses :class:`~arcprompt.StockPromptSource`, which reads
the shipped stock files. arcskill knows arcprompt's lookup contract, never
arcagent and never an agent path.

The judge rubric (``judge_rubric.md``) is a prompt whose *body is YAML* — the same
sign/overlay/version rails as any prose prompt, parsed structured by
:func:`load_rubric` and edited through a dedicated form rather than a textarea.
"""

from __future__ import annotations

from typing import Any

import yaml
from arcprompt import PromptSource

PACKAGE = "arcskill"


def load_prompt(name: str, source: PromptSource) -> str:
    """Return the effective body of the arcskill prompt ``name`` from ``source``.

    Raises whatever the source raises — :class:`~arcprompt.PromptMissing` for a
    prompt arcskill does not ship, :class:`~arcprompt.PromptUnsigned` for a
    tampered override. A broken override is never silently replaced by stock.
    """
    return source.resolve(PACKAGE, name)


def load_rubric(source: PromptSource) -> dict[str, Any]:
    """Load the judge rubric (``judge_rubric.md``) and parse its YAML body.

    The rubric is a structured prompt: dimensions keyed to a ``checklist`` list
    and an ``anti_inflation`` calibration line. Editable through the same
    sign/overlay rails as any prompt, then parsed here into the structure the
    evaluator consumes.
    """
    data = yaml.safe_load(load_prompt("judge_rubric", source))
    if not isinstance(data, dict):
        raise ValueError("judge_rubric body must be a YAML mapping of dimension -> config")
    return data


__all__ = ["PACKAGE", "load_prompt", "load_rubric"]
