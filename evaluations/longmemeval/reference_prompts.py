"""COMP-011 — the reference LongMemEval judge prompts, vendored verbatim.

Vendored from the official LongMemEval repository:

* repo:     https://github.com/xiaowu0162/LongMemEval
* file:     ``src/evaluation/evaluate_qa.py`` (function ``get_anscheck_prompt``)
* revision: ``d6dc8b50a2d9ac0c99485ea28fa5755c62414c34`` ("Update evaluate_qa.py", 2024-11-24)
* raw URL:  https://raw.githubusercontent.com/xiaowu0162/LongMemEval/d6dc8b50a2d9ac0c99485ea28fa5755c62414c34/src/evaluation/evaluate_qa.py
* sha256 as fetched: ``ecce9c4c79dc89d99534ac17b383a5cbb5b9f0c69ee98adaf0684742e3d95251``

The five templates below are byte-for-byte copies of the string literals in that
function, extracted by parsing its ``if``/``elif`` chain rather than by
transcription. Their oddities are the reference's and are deliberately preserved:
the trailing space before ``\\n\\nQuestion:`` in :data:`STANDARD` and
:data:`TEMPORAL_REASONING` but not in the other three, and the ``{}``-positional
(not named) format slots.

**Do not edit any string in this module.** We score with our own judge because
CON-3 requires every LLM call to flow through arcllm, which means the reference
``print_qa_metrics.py`` — it hard-asserts the judge model string — cannot consume
our output. Prompt fidelity is therefore the only remaining basis for
comparability with the published numbers, and ``test_reference_prompts.py`` pins
each template's sha256 so any drift fails the build (REQ-189).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

STANDARD = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."  # noqa: E501 — verbatim vendored; rewrapping would change the bytes we are pinned to.
"""Grader for ``single-session-user``, ``single-session-assistant``, ``multi-session``."""

TEMPORAL_REASONING = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."  # noqa: E501 — verbatim vendored; rewrapping would change the bytes we are pinned to.
"""Grader for ``temporal-reasoning`` — forgives off-by-one day counts."""

KNOWLEDGE_UPDATE = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."  # noqa: E501 — verbatim vendored; rewrapping would change the bytes we are pinned to.
"""Grader for ``knowledge-update`` — superseded facts alongside the new one still pass."""

SINGLE_SESSION_PREFERENCE = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."  # noqa: E501 — verbatim vendored; rewrapping would change the bytes we are pinned to.
"""Grader for ``single-session-preference``.

The second slot is labelled ``Rubric:``, not ``Correct Answer:``, because the
dataset's ``answer`` field for this type *is* a rubric. It is graded, never
string-matched (REQ-191).
"""

ABSTENTION = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."  # noqa: E501 — verbatim vendored; rewrapping would change the bytes we are pinned to.
"""Refusal check applied to every ``_abs`` question id, whatever its base type (REQ-190)."""

TEMPLATES_BY_TYPE: Mapping[str, str] = MappingProxyType(
    {
        "single-session-user": STANDARD,
        "single-session-assistant": STANDARD,
        "multi-session": STANDARD,
        "temporal-reasoning": TEMPORAL_REASONING,
        "knowledge-update": KNOWLEDGE_UPDATE,
        "single-session-preference": SINGLE_SESSION_PREFERENCE,
    }
)
"""The reference ``if``/``elif`` chain as a table. Read-only so no caller can add a type."""


class UnknownQuestionTypeError(ValueError):
    """A question type outside the benchmark's six.

    The reference raises a bare ``NotImplementedError`` here. We refuse just as
    hard, because silently defaulting an unrecognised type to :data:`STANDARD`
    would grade part of the benchmark with the wrong rubric and still report a
    number.
    """


def reference_template(question_type: str, *, is_abstention: bool = False) -> str:
    """Return the reference template for one question, as ``get_anscheck_prompt`` selects it.

    ``is_abstention`` wins over ``question_type``: in the reference the
    abstention branch is the outer ``if``, so an ``_abs`` question is graded on
    refusal regardless of the base type it folds back into (REQ-190).
    """
    if is_abstention:
        return ABSTENTION
    template = TEMPLATES_BY_TYPE.get(question_type)
    if template is None:
        raise UnknownQuestionTypeError(f"no reference judge prompt for type {question_type!r}")
    return template


def reference_prompt(
    *,
    question_type: str,
    question: str,
    answer: str,
    response: str,
    is_abstention: bool = False,
) -> str:
    """Render one judge prompt exactly as the reference's ``template.format`` does.

    ``answer`` is the dataset's gold field verbatim — a literal answer for four
    types, an *explanation* under abstention, and a *rubric* for
    ``single-session-preference``. The template supplies the correct label for
    each; this function never inspects or normalizes it.
    """
    return reference_template(question_type, is_abstention=is_abstention).format(
        question, answer, response
    )


__all__ = [
    "ABSTENTION",
    "KNOWLEDGE_UPDATE",
    "SINGLE_SESSION_PREFERENCE",
    "STANDARD",
    "TEMPLATES_BY_TYPE",
    "TEMPORAL_REASONING",
    "UnknownQuestionTypeError",
    "reference_prompt",
    "reference_template",
]
