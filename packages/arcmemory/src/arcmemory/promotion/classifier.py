"""The ``PromotionClassifier`` seam (SPEC-083 COMP-013).

One question per item: "company, personal, agent_only or unclear?" plus a Noul
cross-check ("this is about the operator's personal life"). ``unclear`` gives
out-of-distribution text a non-company landing spot (decision 5).

The question text IS the versioned contract: :func:`question_version` hashes its
canonical JSON, and a ledger verdict is only reused under the same version.
Only content crosses the seam — never a DID, agent name or workspace path.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal, Protocol

from arctrust import canonical_json
from pydantic import BaseModel, ConfigDict

from arcmemory.promotion.render import PromotableKind

PromotionLabel = Literal["company", "personal", "agent_only", "unclear"]

#: The four scope options, in the order the question lists them.
PROMOTION_LABELS: tuple[PromotionLabel, ...] = ("company", "personal", "agent_only", "unclear")


class ClassifierUnavailableError(Exception):
    """No classifier can be reached (plugin or key missing). Nothing egresses."""


class ClassifierCallError(Exception):
    """The classifier was called and failed (outage, timeout, malformed reply)."""


class ClassifierInput(BaseModel):
    """What is sent to the classifier: the item's kind, id and content — nothing else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_kind: PromotableKind
    item_id: str
    content: str


class ClassifierVerdict(BaseModel):
    """The classifier's answer for one item. Immutable between classify and decide."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: PromotionLabel
    confidence: float
    probabilities: dict[PromotionLabel, float]
    personal_probability: float | None
    classifier_id: str
    classifier_version: str
    request_id: str | None
    input_tokens: int | None


class PromotionClassifier(Protocol):
    """A third-party company-vs-personal classifier behind one async call.

    ``host`` is the egress destination host the content is sent to, recorded on the
    durable ``memory.promotion.egress`` audit; ``None`` when the transport cannot
    name one.
    """

    classifier_id: str
    question_version: str
    host: str | None

    async def classify(self, item: ClassifierInput) -> ClassifierVerdict: ...


def _read_only(tree: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {k: _read_only(v) if isinstance(v, Mapping) else v for k, v in tree.items()}
    )


def _plain(tree: Mapping[str, Any]) -> dict[str, Any]:
    return {str(k): _plain(v) if isinstance(v, Mapping) else v for k, v in tree.items()}


#: The canonical question (SDD COMP-013). Read-only: editing it changes the version.
PROMOTION_QUESTION: Mapping[str, Any] = _read_only(
    {
        "scope": {
            "type": "choice",
            "instructions": "Who is this remembered fact or method useful to?",
            "criteria": {
                "company": {
                    "what": (
                        "The business: company operations, deals (terms, clients, "
                        "counterparties, pricing), market and competitor information, and "
                        "processes or procedures other people in the company could reuse."
                    ),
                    "not_for": "The operator's private life or personal side projects.",
                },
                "personal": {
                    "what": (
                        "The operator's personal life (family, health, home, personal "
                        "finances, hobbies) or their own personal side projects and tinkering."
                    ),
                    "not_for": "Company deals or company processes.",
                },
                "agent_only": {
                    "what": (
                        "Housekeeping only this one assistant needs: its own tool quirks, "
                        "session state, scratch notes, formatting preferences."
                    ),
                },
                "unclear": {
                    "what": (
                        "None of these, not enough information to tell, or text that is "
                        "not a statement about work or personal life."
                    ),
                },
            },
        },
        "personal_check": {
            "type": "noul",
            "instructions": (
                "This text is about the operator's personal life or personal projects."
            ),
        },
    }
)


def question_version(question: Mapping[str, Any]) -> str:
    """``"sha256:"`` + hex digest of the question's canonical JSON (key-order stable)."""
    return "sha256:" + hashlib.sha256(canonical_json(_plain(question))).hexdigest()


__all__ = [
    "PROMOTION_LABELS",
    "PROMOTION_QUESTION",
    "ClassifierCallError",
    "ClassifierInput",
    "ClassifierUnavailableError",
    "ClassifierVerdict",
    "PromotionClassifier",
    "PromotionLabel",
    "question_version",
]
