"""The promotion question, loaded from ``arcmemory/context/promotion_classify.md``.

The question is an operator-editable prompt (SPEC-083 COMP-030): the operator
edits its text in ArcUI, and the agent's ``PromptSource`` answers with the
override or the packaged stock file. This module parses that Markdown strictly
into the Choice + Noul question the classifier sends.

Format (``##`` sections, each exactly once, no text outside a section)::

    ## Instructions
    <the Choice instructions>

    ## Label: company          (one section per label; the four labels are fixed)
    what: <required>
    not_for: <optional>
    examples:                  (optional)
    - <example>

    ## Personal check
    <the Noul statement>

A field value may wrap onto following lines. The labels and the two question
keys (``scope``, ``personal_check``) are fixed by code: a missing or extra
label, an empty ``what``, an unknown section or field, or an unreadable prompt
raises :class:`PromotionQuestionInvalidError`, a
:class:`ClassifierUnavailableError` — the sweep sends nothing and never falls
back to the stock question silently.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from arcprompt import PromptError, PromptSource

from arcmemory.promotion.classifier import (
    PROMOTION_LABELS,
    PromotionQuestionInvalidError,
)

PROMPT_PACKAGE = "arcmemory"
PROMPT_NAME = "promotion_classify"

_INSTRUCTIONS = "Instructions"
_PERSONAL_CHECK = "Personal check"
_LABEL_PREFIX = "Label: "
_FIELDS = ("what", "not_for", "examples")
_FIELD_LINE = re.compile(r"^([a-z_]+):\s*(.*)$")


def load_promotion_question(prompts: PromptSource) -> Mapping[str, Any]:
    """Resolve the effective question through ``prompts`` and parse it strictly.

    Raises:
        PromotionQuestionInvalidError: the prompt cannot be resolved (missing,
            unsigned or tampered override) or does not parse.
    """
    try:
        body = prompts.resolve(PROMPT_PACKAGE, PROMPT_NAME)
    except PromptError as exc:
        raise PromotionQuestionInvalidError(f"prompt unavailable ({type(exc).__name__})") from None
    return parse_promotion_question(body)


def parse_promotion_question(body: str) -> Mapping[str, Any]:
    """Parse the question Markdown into the read-only Choice + Noul question tree.

    Raises:
        PromotionQuestionInvalidError: the body breaks the format above. The
            message names the structural fault only, never operator text.
    """
    sections = _sections(body)
    expected = {
        _INSTRUCTIONS,
        _PERSONAL_CHECK,
        *(_LABEL_PREFIX + label for label in PROMOTION_LABELS),
    }
    if set(sections) != expected:
        missing = sorted(expected - set(sections))
        raise PromotionQuestionInvalidError(
            f"sections must be exactly the fixed set (missing: {missing}, "
            f"unexpected: {len(set(sections) - expected)})"
        )
    criteria = {
        label: _label_criteria(label, sections[_LABEL_PREFIX + label])
        for label in PROMOTION_LABELS
    }
    question = {
        "scope": {
            "type": "choice",
            "instructions": _prose(_INSTRUCTIONS, sections[_INSTRUCTIONS]),
            "criteria": criteria,
        },
        "personal_check": {
            "type": "noul",
            "instructions": _prose(_PERSONAL_CHECK, sections[_PERSONAL_CHECK]),
        },
    }
    return _read_only(question)


def _sections(body: str) -> dict[str, list[str]]:
    """Split ``## heading`` sections; reject text outside a section and repeats."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in body.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            if heading in sections:
                raise PromotionQuestionInvalidError("a section appears more than once")
            current = sections[heading] = []
        elif current is not None:
            current.append(line)
        elif line.strip():
            raise PromotionQuestionInvalidError("text outside a section")
    return sections


def _prose(heading: str, lines: list[str]) -> str:
    text = " ".join(line.strip() for line in lines if line.strip())
    if not text:
        raise PromotionQuestionInvalidError(f"section {heading!r} is empty")
    return text


def _label_criteria(label: str, lines: list[str]) -> dict[str, Any]:
    """One label's ``what`` / ``not_for`` / ``examples`` fields."""
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        match = _FIELD_LINE.match(line)
        if match is not None:
            current = _start_field(label, fields, match.group(1))
            if match.group(2):
                fields[current].append(match.group(2))
        elif current is None:
            raise PromotionQuestionInvalidError(f"label {label!r} has text before a field")
        else:
            fields[current].append(line)
    return _criteria_of(label, fields)


def _start_field(label: str, fields: dict[str, list[str]], name: str) -> str:
    if name not in _FIELDS:
        raise PromotionQuestionInvalidError(f"label {label!r} has an unknown field")
    if name in fields:
        raise PromotionQuestionInvalidError(f"label {label!r} repeats field {name!r}")
    fields[name] = []
    return name


def _criteria_of(label: str, fields: dict[str, list[str]]) -> dict[str, Any]:
    what = " ".join(fields.get("what", []))
    if not what:
        raise PromotionQuestionInvalidError(f"label {label!r} has an empty 'what'")
    criteria: dict[str, Any] = {"what": what}
    if "not_for" in fields:
        not_for = " ".join(fields["not_for"])
        if not not_for:
            raise PromotionQuestionInvalidError(f"label {label!r} has an empty 'not_for'")
        criteria["not_for"] = not_for
    if "examples" in fields:
        criteria["examples"] = _examples(label, fields["examples"])
    return criteria


def _examples(label: str, lines: list[str]) -> list[str]:
    items = [line[2:].strip() for line in lines if line.startswith("- ")]
    if not items or len(items) != len(lines) or not all(items):
        raise PromotionQuestionInvalidError(
            f"label {label!r} examples must be a non-empty '- ' list"
        )
    return items


def _read_only(tree: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {k: _read_only(v) if isinstance(v, Mapping) else v for k, v in tree.items()}
    )


__all__ = [
    "PROMPT_NAME",
    "PROMPT_PACKAGE",
    "load_promotion_question",
    "parse_promotion_question",
]
