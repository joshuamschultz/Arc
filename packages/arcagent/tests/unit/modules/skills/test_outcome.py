"""SPEC-054 T-727 (RED) — turn-end outcome classifier (PRD REQ-115/116, SDD COMP-006).

Pins the API of ``arcagent.modules.skills.outcome`` before it exists:

* ``has_feedback_signal(messages) -> bool`` — PURE heuristic pre-filter over the turn's
  user-visible messages (``[{"role": str, "content": str}, ...]``). True on correction
  phrases, an immediate re-ask (exact user repeat in the window), negative-sentiment
  lexicon hits, praise markers, or policy-risk praise. No-signal turns never reach the LLM.
* ``has_policy_risk_praise(messages) -> bool`` — PURE lexicon check: praise of content
  that names a guardrail evasion (skip/bypass/ignore/disable + check/policy/permission/
  sandbox/approval/validation). A 'success' produced alongside this is downgraded to ``''``.
* ``OutcomeLabel`` — Pydantic model: ``outcome`` in {'success','failure','partial',''},
  ``skill: str | None``.
* ``OutcomeClassifier(llm=<LLMInvoker: async invoke(prompt) -> str>)`` with
  ``async classify(*, transcript_window, active_skills, error_counts) -> OutcomeLabel``.
  The LLM must answer with JSON ``{"outcome": ..., "skill": ...}``; anything malformed
  abstains (``''``) — the hook path stays fail-open. Failure is NEVER emitted without a
  skill attribution in ``active_skills``; when the LLM abstains on attribution, the
  fallback credits 'partial' to the unique skill with the strictly highest error count
  (tie or all-zero -> abstain).
* Wiring — ``_runtime._State`` grows ``outcome_classifier: OutcomeClassifier | None``
  (default ``None``); ``SkillsConfig`` grows ``classify_outcomes: bool = False``;
  ``configure(classify_outcomes=True)`` builds the classifier from the eval LLM;
  ``skills_post_plan`` reads the window from ``ctx.data['messages']`` and forwards the
  produced label into ``adapter.on_turn_end(outcome=...)``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcagent.modules.skills.outcome import (
    OutcomeClassifier,
    OutcomeLabel,
    has_feedback_signal,
    has_policy_risk_praise,
)
from pydantic import ValidationError

from arcagent.capabilities.capability_registry import CapabilityRegistry, SkillEntry
from arcagent.modules.skills import _runtime
from arcagent.modules.skills.capabilities import skills_post_plan, skills_post_tool, skills_ready
from arcagent.modules.skills.config import SkillsConfig

# ---------------------------------------------------------------------------- fixtures


def _user(content: str) -> dict[str, str]:
    return {"role": "user", "content": content}


def _assistant(content: str) -> dict[str, str]:
    return {"role": "assistant", "content": content}


BLAND_TURN = [
    _user("now update the readme with the new install steps"),
    _assistant("Updated the readme."),
]

CORRECTION_TURN = [
    _assistant("I converted the totals to dollars."),
    _user("no, that's wrong — I asked for the totals in euros"),
]

RISKY_PRAISE_TURN = [
    _assistant("Done."),
    _user("perfect — bypassing the sandbox check like that saved so much time"),
]

PRAISE_TURN = [
    _assistant("Done."),
    _user("thanks, that worked perfectly"),
]


class _FakeLLM:
    """Scripted LLMInvoker seam: async invoke(prompt) -> str, recording every call."""

    def __init__(self, responses: list[str] | None = None, error: Exception | None = None):
        self.calls: list[str] = []
        self._responses = list(responses or [])
        self._error = error

    async def invoke(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self._error is not None:
            raise self._error
        return self._responses.pop(0) if self._responses else "{}"


class _FakeAdapter:
    """Records the on_turn_end forwarding surface the hooks drive."""

    def __init__(self) -> None:
        self.observations: list[dict[str, Any]] = []
        self.turn_ends: list[dict[str, Any]] = []

    async def observe(self, **kwargs: Any) -> None:
        self.observations.append(kwargs)

    async def on_turn_end(self, *, turn: int, outcome: str) -> None:
        self.turn_ends.append({"turn": turn, "outcome": outcome})


class _Ctx:
    def __init__(self, **data: Any) -> None:
        self.data = data
        self.is_vetoed = False


async def _registry(*skills: tuple[str, Path]) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    for name, loc in skills:
        await reg.register_skill(
            SkillEntry(
                name=name,
                version="1.0.0",
                description=name,
                triggers=(),
                tools=(),
                location=loc,
                scan_root="builtin",
            )
        )
    return reg


@pytest.fixture(autouse=True)
def _clean_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


# ------------------------------------------------------- heuristic pre-filter (REQ-115)


def test_has_feedback_signal_empty_window_is_false() -> None:
    assert has_feedback_signal([]) is False


def test_has_feedback_signal_bland_turn_is_false() -> None:
    assert has_feedback_signal(BLAND_TURN) is False


def test_has_feedback_signal_correction_phrase_is_true() -> None:
    assert has_feedback_signal(CORRECTION_TURN) is True


def test_has_feedback_signal_immediate_reask_repeat_is_true() -> None:
    reask = [
        _user("convert the report to csv"),
        _assistant("Here is the converted report."),
        _user("convert the report to csv"),
    ]
    assert has_feedback_signal(reask) is True


def test_has_feedback_signal_negative_sentiment_is_true() -> None:
    turn = [_user("this is still broken and the export doesn't work")]
    assert has_feedback_signal(turn) is True


def test_has_feedback_signal_praise_is_true() -> None:
    # Positive implicit feedback is a candidate signal too — it is the only way a
    # 'success' credit (REQ-116) can ever be produced by the LLM stage.
    assert has_feedback_signal(PRAISE_TURN) is True


def test_has_policy_risk_praise_lexicon() -> None:
    assert has_policy_risk_praise(RISKY_PRAISE_TURN) is True
    assert has_policy_risk_praise(PRAISE_TURN) is False
    assert has_policy_risk_praise([_user("please rename the folder")]) is False


# ------------------------------------------------------------------- label boundary


def test_outcome_label_rejects_unknown_outcome() -> None:
    with pytest.raises(ValidationError):
        OutcomeLabel(outcome="amazing", skill=None)


# ------------------------------------------------------- pre-filter gating (REQ-115)


@pytest.mark.asyncio
async def test_bland_turn_makes_no_llm_call_and_yields_empty_outcome() -> None:
    llm = _FakeLLM()
    classifier = OutcomeClassifier(llm=llm)
    label = await classifier.classify(
        transcript_window=BLAND_TURN, active_skills=["csv-parser"], error_counts={}
    )
    assert llm.calls == []
    assert label.outcome == ""
    assert label.skill is None


@pytest.mark.asyncio
async def test_correction_turn_triggers_exactly_one_llm_call() -> None:
    llm = _FakeLLM(responses=['{"outcome": "failure", "skill": "csv-parser"}'])
    classifier = OutcomeClassifier(llm=llm)
    await classifier.classify(
        transcript_window=CORRECTION_TURN, active_skills=["csv-parser"], error_counts={}
    )
    assert len(llm.calls) == 1


# ------------------------------------------------------------- attribution (REQ-116)


@pytest.mark.asyncio
async def test_failure_binds_to_named_active_skill() -> None:
    llm = _FakeLLM(responses=['{"outcome": "failure", "skill": "csv-parser"}'])
    classifier = OutcomeClassifier(llm=llm)
    label = await classifier.classify(
        transcript_window=CORRECTION_TURN,
        active_skills=["csv-parser", "web-search"],
        error_counts={},
    )
    assert label.outcome == "failure"
    assert label.skill == "csv-parser"


@pytest.mark.asyncio
async def test_unknown_skill_attribution_abstains_never_unattributed_failure() -> None:
    llm = _FakeLLM(responses=['{"outcome": "failure", "skill": "not-an-active-skill"}'])
    classifier = OutcomeClassifier(llm=llm)
    label = await classifier.classify(
        transcript_window=CORRECTION_TURN,
        active_skills=["csv-parser", "web-search"],
        error_counts={},
    )
    assert label.outcome != "failure"
    assert label.skill is None
    assert label.outcome == ""


@pytest.mark.asyncio
async def test_llm_abstain_falls_back_to_error_count_credit_split() -> None:
    # Pinned fallback: LLM abstains on attribution -> 'partial' credited to the UNIQUE
    # skill with the strictly highest error count among the turn's active skills.
    llm = _FakeLLM(responses=['{"outcome": "failure", "skill": null}'])
    classifier = OutcomeClassifier(llm=llm)
    label = await classifier.classify(
        transcript_window=CORRECTION_TURN,
        active_skills=["csv-parser", "web-search"],
        error_counts={"csv-parser": 2, "web-search": 0},
    )
    assert label.outcome == "partial"
    assert label.skill == "csv-parser"


@pytest.mark.asyncio
async def test_llm_abstain_with_tied_error_counts_abstains() -> None:
    llm = _FakeLLM(responses=['{"outcome": "failure", "skill": null}'])
    classifier = OutcomeClassifier(llm=llm)
    label = await classifier.classify(
        transcript_window=CORRECTION_TURN,
        active_skills=["csv-parser", "web-search"],
        error_counts={"csv-parser": 1, "web-search": 1},
    )
    assert label.outcome == ""
    assert label.skill is None


# ------------------------------------------------------------------ silence (REQ-116)


@pytest.mark.asyncio
async def test_silent_turn_never_yields_failure() -> None:
    llm = _FakeLLM(responses=['{"outcome": "failure", "skill": "csv-parser"}'])
    classifier = OutcomeClassifier(llm=llm)
    for window in ([], [_assistant("I finished the task.")]):
        label = await classifier.classify(
            transcript_window=window, active_skills=["csv-parser"], error_counts={}
        )
        assert label.outcome in ("", "success")
        assert label.outcome != "failure"
    assert llm.calls == []


# --------------------------------------------------- safety check on positives (REQ-116)


@pytest.mark.asyncio
async def test_plain_praise_yields_success_bound_to_skill() -> None:
    llm = _FakeLLM(responses=['{"outcome": "success", "skill": "csv-parser"}'])
    classifier = OutcomeClassifier(llm=llm)
    label = await classifier.classify(
        transcript_window=PRAISE_TURN, active_skills=["csv-parser"], error_counts={}
    )
    assert label.outcome == "success"
    assert label.skill == "csv-parser"


@pytest.mark.asyncio
async def test_policy_risk_praise_downgrades_success_to_empty() -> None:
    llm = _FakeLLM(responses=['{"outcome": "success", "skill": "shell-helper"}'])
    classifier = OutcomeClassifier(llm=llm)
    label = await classifier.classify(
        transcript_window=RISKY_PRAISE_TURN, active_skills=["shell-helper"], error_counts={}
    )
    assert label.outcome == ""
    assert label.skill is None


# ---------------------------------------------------------- malformed output (fail-open)


@pytest.mark.asyncio
async def test_non_json_llm_output_abstains() -> None:
    llm = _FakeLLM(responses=["I think the task went really well!"])
    classifier = OutcomeClassifier(llm=llm)
    label = await classifier.classify(
        transcript_window=CORRECTION_TURN, active_skills=["csv-parser"], error_counts={}
    )
    assert label.outcome == ""
    assert label.skill is None


@pytest.mark.asyncio
async def test_missing_fields_llm_output_abstains() -> None:
    llm = _FakeLLM(responses=['{"skill": "csv-parser"}'])
    classifier = OutcomeClassifier(llm=llm)
    label = await classifier.classify(
        transcript_window=CORRECTION_TURN, active_skills=["csv-parser"], error_counts={}
    )
    assert label.outcome == ""


@pytest.mark.asyncio
async def test_invalid_outcome_value_abstains() -> None:
    llm = _FakeLLM(responses=['{"outcome": "amazing", "skill": "csv-parser"}'])
    classifier = OutcomeClassifier(llm=llm)
    label = await classifier.classify(
        transcript_window=CORRECTION_TURN, active_skills=["csv-parser"], error_counts={}
    )
    assert label.outcome == ""
    assert label.skill is None


@pytest.mark.asyncio
async def test_llm_exception_abstains_fail_open() -> None:
    llm = _FakeLLM(error=RuntimeError("provider down"))
    classifier = OutcomeClassifier(llm=llm)
    label = await classifier.classify(
        transcript_window=CORRECTION_TURN, active_skills=["csv-parser"], error_counts={}
    )
    assert label.outcome == ""
    assert label.skill is None


# ---------------------------------------------------------------- hook wiring (REQ-115)


@pytest.mark.asyncio
async def test_configured_classifier_forwards_label_into_on_turn_end(tmp_path: Path) -> None:
    """skills_post_plan consults the classifier and forwards its label to the adapter."""
    skill_file = tmp_path / "my-skill" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("# my-skill\n", encoding="utf-8")

    adapter = _FakeAdapter()
    llm = _FakeLLM(responses=['{"outcome": "failure", "skill": "my-skill"}'])
    state = _runtime._State(
        adapter=adapter,  # type: ignore[arg-type]  # duck-typed fake records calls
        active=True,
        workspace=tmp_path,
        outcome_classifier=OutcomeClassifier(llm=llm),
    )
    _runtime.bind(state)

    await skills_ready(_Ctx(skill_registry=await _registry(("my-skill", skill_file))))
    await skills_post_tool(_Ctx(tool="read", args={"file_path": str(skill_file)}))
    await skills_post_plan(_Ctx(turn_number=3, messages=CORRECTION_TURN))

    assert len(llm.calls) == 1
    assert adapter.turn_ends == [{"turn": 3, "outcome": "failure"}]


@pytest.mark.asyncio
async def test_without_classifier_hooks_behave_exactly_as_today(tmp_path: Path) -> None:
    adapter = _FakeAdapter()
    state = _runtime._State(
        adapter=adapter,  # type: ignore[arg-type]  # duck-typed fake records calls
        active=True,
        workspace=tmp_path,
    )
    _runtime.bind(state)

    await skills_post_plan(_Ctx(turn_number=1, messages=CORRECTION_TURN))

    assert adapter.turn_ends == [{"turn": 1, "outcome": ""}]


def test_config_bool_defaults_off_and_configure_wires_classifier(tmp_path: Path) -> None:
    assert SkillsConfig().classify_outcomes is False

    _runtime.configure(config={"adapter": "arcskill"}, workspace=tmp_path)
    assert _runtime.state().outcome_classifier is None

    _runtime.configure(
        config={"adapter": "arcskill", "classify_outcomes": True}, workspace=tmp_path
    )
    assert _runtime.state().outcome_classifier is not None
