"""The judge — COMP-010 / REQ-188, REQ-189, REQ-190, REQ-191.

Three things are asserted here and each one is load-bearing on its own:

* the live prompt the judge sends is byte-identical to the vendored reference
  template (REQ-189) — the diff test COMP-011 exists for;
* the judge is a genuinely separate agent, pinned to the dated model id, with
  its own key variable (REQ-188);
* the prompt variant follows the question, including the rubric path that
  ~6% of the benchmark depends on (REQ-190, REQ-191).

Nothing here starts an agent, makes a network or LLM call, or writes outside
`tmp_path`. The judge's LLM seam is a recording stub, so the real prompt
selection and label parsing run for real.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

import pytest

from evaluations.longmemeval.ingest.agent_factory import (
    ARCLLM_EVAL_CONFIG,
    eval_agent_name,
    render_eval_agent_config,
)
from evaluations.longmemeval import judge as judge_mod
from evaluations.longmemeval import reference_prompts as rp
from evaluations.longmemeval.judge import (
    JUDGE_AGENT_NAME,
    JUDGE_API_KEY_ENV,
    JUDGE_ARCLLM_CONFIG,
    JUDGE_ARCRUN_CONFIG,
    JUDGE_MAX_TOKENS,
    JUDGE_MODEL_ID,
    JUDGE_MODEL_NAME,
    JudgeAgent,
    JudgeKeyMissingError,
    Verdict,
    install_judge_api_key,
    is_abstention_question,
    judge_dir,
    judge_prompt,
    judge_session_key,
    parse_label,
    render_judge_agent_config,
    write_judge_agent_config,
)

QUESTION_TYPES = sorted(rp.TEMPLATES_BY_TYPE)


class RecordingRunner:
    """Stands in for the judge's `ArcAgent`, recording what it was asked.

    Deliberately not a mock of the whole agent: the judge only ever drives one
    turn on one keyed session, and a stub that offers exactly that is what keeps
    the test honest about the seam it is exercising.
    """

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.prompts: list[str] = []
        self.session_keys: list[str] = []

    async def run_collected(self, input_text: str, *, session_key: str) -> Any:
        self.prompts.append(input_text)
        self.session_keys.append(session_key)
        return _Result(self._reply)


class _Result:
    def __init__(self, content: str) -> None:
        self.content = content


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    path = (tmp_path / "runs" / "lme-q0001").resolve()
    path.mkdir(parents=True)
    return path


# ---------------------------------------------------------------------------
# REQ-189 — the byte-diff fidelity test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("question_type", QUESTION_TYPES)
@pytest.mark.parametrize("question_id", ["q0001", "q0001_abs"])
def test_live_prompt_is_byte_identical_to_the_vault(question_type: str, question_id: str) -> None:
    """Diff what the judge actually sends against the vendored template, byte for byte."""
    live = judge_prompt(
        question_id=question_id,
        question_type=question_type,
        question="What did I say about the boiler?",
        gold="It needed a new thermostat.",
        answer="You mentioned the thermostat had to be replaced.",
    )
    template = rp.reference_template(
        question_type, is_abstention=is_abstention_question(question_id)
    )
    expected = template.format(
        "What did I say about the boiler?",
        "It needed a new thermostat.",
        "You mentioned the thermostat had to be replaced.",
    )
    if live != expected:
        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(), live.splitlines(), "vault", "live", lineterm=""
            )
        )
        pytest.fail(f"judge prompt has drifted from the vendored reference:\n{diff}")
    assert live.encode("utf-8") == expected.encode("utf-8")


# ---------------------------------------------------------------------------
# REQ-188 — model pinning, decoding knobs, and its own key variable
# ---------------------------------------------------------------------------


def test_model_id_is_the_dated_string_never_the_alias() -> None:
    """The `gpt-4o` alias silently repoints, which would move every number."""
    assert JUDGE_MODEL_ID == "openai/gpt-4o-2024-08-06"
    assert JUDGE_MODEL_NAME == "gpt-4o-2024-08-06"
    assert JUDGE_MODEL_ID.rsplit("/", 1)[-1] != "gpt-4o"


def test_judge_arcllm_config_pins_all_three_decoding_knobs() -> None:
    lines = JUDGE_ARCLLM_CONFIG.splitlines()
    assert any(line.startswith(f'model = "{JUDGE_MODEL_ID}"') for line in lines)
    assert any(line.startswith(f"max_tokens = {JUDGE_MAX_TOKENS}") for line in lines)
    assert any(line.startswith("temperature = 0.0") for line in lines)
    assert "store_raw_bodies = false" in JUDGE_ARCLLM_CONFIG


def test_judge_key_variable_is_its_own_and_read_from_the_environment_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert JUDGE_API_KEY_ENV == "LME_JUDGE_OPENAI_API_KEY"
    assert JUDGE_API_KEY_ENV != judge_mod._PROVIDER_API_KEY_ENV
    assert JUDGE_API_KEY_ENV != "ANTHROPIC_API_KEY"

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{JUDGE_API_KEY_ENV}=from-a-file\n", encoding="utf-8")
    monkeypatch.setenv(JUDGE_API_KEY_ENV, "from-the-environment")
    monkeypatch.delenv(judge_mod._PROVIDER_API_KEY_ENV, raising=False)

    assert install_judge_api_key() == "from-the-environment"


def test_missing_judge_key_fails_loudly_instead_of_borrowing_the_provider_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(JUDGE_API_KEY_ENV, raising=False)
    monkeypatch.setenv(judge_mod._PROVIDER_API_KEY_ENV, "the-system-under-test-key")
    with pytest.raises(JudgeKeyMissingError):
        install_judge_api_key()


def test_judge_shares_no_config_directory_workspace_or_name_with_the_system_under_test(
    run_dir: Path,
) -> None:
    sut_dir = run_dir / "agent"
    assert judge_dir(run_dir) != sut_dir
    assert not judge_dir(run_dir).is_relative_to(sut_dir)
    assert not sut_dir.is_relative_to(judge_dir(run_dir))
    assert JUDGE_AGENT_NAME != eval_agent_name("q0001")


def test_judge_and_system_under_test_share_no_llm_config(run_dir: Path) -> None:
    """Different model, different decoding — the two configs are not the same dict."""
    assert JUDGE_ARCLLM_CONFIG != ARCLLM_EVAL_CONFIG
    assert JUDGE_MODEL_ID not in ARCLLM_EVAL_CONFIG
    assert render_judge_agent_config(run_dir=run_dir) != render_eval_agent_config(
        question_id="q0001", run_dir=run_dir
    )


def test_judge_runs_no_modules(run_dir: Path) -> None:
    """A judge that remembered its gradings would carry gold between questions."""
    rendered = render_judge_agent_config(run_dir=run_dir)
    in_module = False
    for line in rendered.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_module = stripped.startswith("[modules.")
            continue
        if in_module:
            assert stripped != "enabled = true", f"module still enabled: {line}"
    assert "[modules.memory]" in rendered


def test_judge_loop_may_call_no_tools() -> None:
    """`[]` is set-and-empty (nothing allowed), not the unset default (all allowed)."""
    assert JUDGE_ARCRUN_CONFIG.rstrip().endswith("allowed_tools = []  # a grader calls nothing")
    assert JUDGE_ARCRUN_CONFIG.index("[sandbox]") < JUDGE_ARCRUN_CONFIG.index("allowed_tools")


def test_render_refuses_a_relative_run_dir() -> None:
    with pytest.raises(ValueError, match="absolute"):
        render_judge_agent_config(run_dir=Path("runs/lme-q0001"))


def test_write_stays_inside_the_run_dir(run_dir: Path) -> None:
    config_path = write_judge_agent_config(run_dir=run_dir)
    assert config_path == judge_dir(run_dir) / "arcagent.toml"
    for sibling in ("arcllm.toml", "arcrun.toml"):
        assert (judge_dir(run_dir) / sibling).is_file()
    assert (judge_dir(run_dir) / "workspace" / "identity.md").is_file()
    for path in run_dir.rglob("*"):
        assert path.is_relative_to(run_dir)


def test_each_question_is_judged_on_its_own_session(run_dir: Path) -> None:
    keys = {judge_session_key(f"q{i:04d}") for i in range(3)}
    assert len(keys) == 3
    assert all(key.startswith("judge:") for key in keys)


# ---------------------------------------------------------------------------
# REQ-190 / REQ-191 — prompt variant selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("question_type", QUESTION_TYPES)
async def test_abs_question_ids_get_the_refusal_prompt_whatever_the_base_type(
    question_type: str,
) -> None:
    runner = RecordingRunner("no")
    verdict = await JudgeAgent(runner).judge(
        question_id="q0042_abs",
        question_type=question_type,
        question="When did I cancel the gym membership?",
        gold="The user never mentioned cancelling a gym membership.",
        answer="You cancelled it in March.",
    )
    assert runner.prompts == [
        rp.ABSTENTION.format(
            "When did I cancel the gym membership?",
            "The user never mentioned cancelling a gym membership.",
            "You cancelled it in March.",
        )
    ]
    assert "unanswerable" in runner.prompts[0]
    assert verdict.correct is False


def test_abstention_is_detected_as_a_substring_like_the_reference() -> None:
    assert is_abstention_question("q0042_abs")
    assert is_abstention_question("q0042_abs_2")
    assert not is_abstention_question("q0042")


async def test_preference_answer_is_graded_as_a_rubric_and_never_string_matched() -> None:
    """REQ-191 — the dataset `answer` for this type is a rubric, not a literal string.

    Two things are proved: the gold text reaches the judge under the `Rubric:`
    label rather than `Correct Answer:`, and the verdict comes from the judge's
    reply even though the answer shares no wording with the rubric at all. Any
    local string or fuzzy match would have to score this one wrong.
    """
    rubric = "Mentions the user is vegetarian and avoids recommending meat dishes."
    answer = "How about the aubergine parmigiana, or the chickpea stew?"
    runner = RecordingRunner("yes")

    verdict = await JudgeAgent(runner).judge(
        question_id="q0100",
        question_type="single-session-preference",
        question="What should I cook tonight?",
        gold=rubric,
        answer=answer,
    )

    sent = runner.prompts[0]
    assert f"\n\nRubric: {rubric}\n\n" in sent
    assert "Correct Answer:" not in sent
    assert sent == rp.SINGLE_SESSION_PREFERENCE.format(
        "What should I cook tonight?", rubric, answer
    )
    # No overlapping content word — a fuzzy matcher would call this a miss.
    assert not set(rubric.lower().split()) & {"aubergine", "parmigiana", "chickpea", "stew"}
    assert verdict.correct is True


@pytest.mark.parametrize("question_type", QUESTION_TYPES)
async def test_non_abstention_questions_get_their_own_type_template(question_type: str) -> None:
    runner = RecordingRunner("yes")
    await JudgeAgent(runner).judge(
        question_id="q0007",
        question_type=question_type,
        question="Q",
        gold="G",
        answer="A",
    )
    assert runner.prompts == [rp.TEMPLATES_BY_TYPE[question_type].format("Q", "G", "A")]


# ---------------------------------------------------------------------------
# The verdict itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "expected"),
    [("yes", True), ("Yes", True), ("  YES\n", True), ("no", False), ("No.", False), ("", False)],
)
def test_label_is_read_back_the_way_the_reference_reads_it(reply: str, expected: bool) -> None:
    assert parse_label(reply) is expected


async def test_verdict_carries_the_exact_prompt_and_the_dated_model_id() -> None:
    runner = RecordingRunner("Yes\n")
    verdict = await JudgeAgent(runner).judge(
        question_id="q0007",
        question_type="knowledge-update",
        question="Where do I work now?",
        gold="At the hospital.",
        answer="You used to be at the clinic; now you work at the hospital.",
    )
    assert isinstance(verdict, Verdict)
    assert verdict.correct is True
    assert verdict.raw_response == "Yes\n"
    assert verdict.prompt_used == runner.prompts[0]
    assert verdict.judge_model_id == JUDGE_MODEL_NAME
    assert runner.session_keys == ["judge:q0007"]


async def test_verdict_is_immutable() -> None:
    """A graded row is evidence; nothing downstream gets to relabel it in place."""
    verdict = await JudgeAgent(RecordingRunner("yes")).judge(
        question_id="q0007",
        question_type="multi-session",
        question="Q",
        gold="G",
        answer="A",
    )
    with pytest.raises(ValueError, match="frozen"):
        verdict.correct = False
