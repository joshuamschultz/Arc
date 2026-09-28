"""The ``PromotionClassifier`` seam (SPEC-083 COMP-013).

One question per item: "company, personal, agent_only or unclear?" plus a Noul
cross-check ("this is about the operator's personal life"). ``unclear`` gives
out-of-distribution text a non-company landing spot (decision 5).

The question lives in ``arcmemory/context/promotion_classify.md`` (operator-editable,
parsed strictly by :mod:`arcmemory.promotion.question`). Its parsed text IS the
versioned contract: :func:`question_version` hashes its canonical JSON, and a
ledger verdict is only reused under the same version.
Only content crosses the seam — never a DID, agent name or workspace path.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from arctrust import canonical_json
from pydantic import BaseModel, ConfigDict

from arcmemory.promotion.render import PromotableKind

PromotionLabel = Literal["company", "personal", "agent_only", "unclear"]

#: The four scope options, in the order the question lists them.
PROMOTION_LABELS: tuple[PromotionLabel, ...] = ("company", "personal", "agent_only", "unclear")


class ClassifierUnavailableError(Exception):
    """No classifier can serve (plugin, SDK or key missing). Nothing egresses."""


class PromotionQuestionInvalidError(ClassifierUnavailableError):
    """The promotion question prompt cannot be resolved or does not parse.

    A :class:`ClassifierUnavailableError`: nothing is sent, and the stock question
    is never substituted silently. ``prompt`` names the prompt for the audit.
    """

    prompt = "arcmemory/promotion_classify"


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

    ``ensure_available`` is a network-free preflight the sweep runs before it
    plans or audits any egress: it raises :class:`ClassifierUnavailableError`
    when the classifier cannot serve, and sends nothing.
    """

    classifier_id: str
    host: str | None

    @property
    def question_version(self) -> str:
        """``"sha256:"`` digest of the question this classifier currently asks."""
        ...

    async def ensure_available(self) -> None: ...

    async def classify(self, item: ClassifierInput) -> ClassifierVerdict: ...


def _plain(tree: Mapping[str, Any]) -> dict[str, Any]:
    return {str(k): _plain(v) if isinstance(v, Mapping) else v for k, v in tree.items()}


def question_version(question: Mapping[str, Any]) -> str:
    """``"sha256:"`` + hex digest of the question's canonical JSON (key-order stable)."""
    return "sha256:" + hashlib.sha256(canonical_json(_plain(question))).hexdigest()


__all__ = [
    "PROMOTION_LABELS",
    "ClassifierCallError",
    "ClassifierInput",
    "ClassifierUnavailableError",
    "ClassifierVerdict",
    "PromotionClassifier",
    "PromotionLabel",
    "PromotionQuestionInvalidError",
    "question_version",
]
