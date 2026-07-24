"""Prompt templates arcskill ships — loaded standalone, optionally arc-managed.

arcskill houses the improver prompts it ships (``context/<name>.md``) and reads
them with this small dependency-free loader, so **the package works with no
arcprompt installed** — the default path is a plain stock read.

When the arc system IS present, the consumer (arcagent) passes a ``resolve``
callable that routes through arcprompt's overlay-aware, signed resolution. arcskill
never imports arcprompt: it merely *uses* the resolver it is handed. So a signed
operator override authored through arcui/CLI takes effect here, while a bare
arcskill still loads its shipped defaults. The ``.md`` format matches arcprompt's
(YAML frontmatter + body, one trailing newline stripped) so arcprompt's catalog
discovers, views, and versions these same files.

The judge rubric (``judge_rubric.md``) is a prompt whose *body is YAML* — the same
sign/overlay/version rails as any prose prompt, parsed structured by
:func:`load_rubric` and edited through a dedicated form rather than a textarea.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

# (package, name) -> effective body. Supplied by arcagent (arcprompt-backed) so
# overrides take effect; ``None`` means "no arc system present" → stock read.
PromptResolve = Callable[[str, str], str]

_FRONTMATTER_DELIM = "---\n"


class PromptMissingError(FileNotFoundError):
    """Raised when a named prompt file is not packaged under ``context/``."""


def _stock_body(name: str) -> str:
    """Read ``context/<name>.md`` from the installed package, frontmatter stripped."""
    path = Path(__file__).parent / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptMissingError(f"arcskill ships no prompt named {name!r}") from exc
    if not text.startswith(_FRONTMATTER_DELIM):
        raise ValueError(f"prompt {name!r} does not begin with a '---' frontmatter block")
    rest = text[len(_FRONTMATTER_DELIM) :]
    end = rest.find("\n" + _FRONTMATTER_DELIM)
    if end == -1:
        raise ValueError(f"prompt {name!r} frontmatter block is not terminated by a '---' line")
    body = rest[end + len("\n" + _FRONTMATTER_DELIM) :]
    if body.endswith("\n"):
        body = body[:-1]
    return body


def load_prompt(name: str, *, resolve: PromptResolve | None = None) -> str:
    """Return the effective body of an arcskill prompt.

    With ``resolve`` (the arc system is present): route through arcprompt's
    overlay-aware, signature-verified resolution — an operator override wins.
    Without it (standalone): read the shipped stock file directly.
    """
    if resolve is not None:
        return resolve("arcskill", name)
    return _stock_body(name)


def load_rubric(*, resolve: PromptResolve | None = None) -> dict[str, Any]:
    """Load the judge rubric (``judge_rubric.md``) and parse its YAML body.

    The rubric is a structured prompt: dimensions keyed to a ``checklist`` list
    and an ``anti_inflation`` calibration line. Editable through the same
    sign/overlay rails as any prompt, then parsed here into the structure the
    evaluator consumes.
    """
    body = load_prompt("judge_rubric", resolve=resolve)
    data = yaml.safe_load(body)
    if not isinstance(data, dict):
        raise ValueError("judge_rubric body must be a YAML mapping of dimension -> config")
    return data


__all__ = ["PromptMissingError", "PromptResolve", "load_prompt", "load_rubric"]
