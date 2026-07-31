"""COMP-010 — the judge: a second, separate Arc agent that scores one answer.

Grading runs through arcagent → arcrun → arcllm like every other LLM call in
this repository (CON-3), on an agent that is *not* the system under test: its
own config directory, its own workspace, its own model, its own key variable and
its own sessions. Nothing but the prompt text crosses between them.

The three decoding knobs are pinned to the reference's
(``temperature = 0``, ``max_tokens = 10``, model ``gpt-4o-2024-08-06``) and the
model id is the dated string, never the ``gpt-4o`` alias — the alias silently
repoints, which would move every published-comparison number under us without a
diff (REQ-188). The prompts come verbatim from
:mod:`evaluations.longmemeval.reference_prompts`; this module only picks the
variant and reads the label back the way the reference does.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol

from arccli.commands.agent import _scaffold_workspace
from arccli.commands.agent._common import (
    _DEFAULT_ARCRUN_CONFIG,
    render_agent_config,
)
from pydantic import BaseModel, ConfigDict

from evaluations.ingest.agent_factory import (
    ARCLLM_EVAL_CONFIG,
    _apply_toml_overrides,
    assert_workspace_contained,
)
from evaluations.longmemeval.reference_prompts import reference_prompt

JUDGE_MODEL_ID = "openai/gpt-4o-2024-08-06"
"""The arcllm model id. Dated, never the ``gpt-4o`` alias (REQ-188)."""

JUDGE_MODEL_NAME = "gpt-4o-2024-08-06"
"""The provider-side model string, recorded on every :class:`Verdict`."""

JUDGE_TEMPERATURE = 0.0
JUDGE_MAX_TOKENS = 10

JUDGE_API_KEY_ENV = "LME_JUDGE_OPENAI_API_KEY"
"""The judge's own key variable — read from the environment, never from a file.

A separate name from the provider default is the point: the operator can give
the judge a key the system under test has no way to reach, and a missing judge
key fails loudly instead of quietly borrowing whatever ``OPENAI_API_KEY``
happens to be exported.
"""

_PROVIDER_API_KEY_ENV = "OPENAI_API_KEY"
"""Where arcllm looks. ``api_key_env`` is packaged per provider and is not
per-agent overridable, so :func:`install_judge_api_key` is the bridge from the
judge's variable to the slot the openai adapter reads at construction."""

JUDGE_AGENT_NAME = "lme-judge"
JUDGE_DIR_NAME = "judge"


class JudgeKeyMissingError(RuntimeError):
    """``LME_JUDGE_OPENAI_API_KEY`` is unset or empty in the environment."""


class Verdict(BaseModel):
    """One graded answer.

    ``prompt_used`` is the fully rendered prompt, not a template name: when a
    number is disputed months later, the only useful artifact is the exact text
    the judge saw.
    """

    model_config = ConfigDict(frozen=True)

    correct: bool
    raw_response: str
    prompt_used: str
    judge_model_id: str


def is_abstention_question(question_id: str) -> bool:
    """Whether a question id marks an unanswerable (abstention) item.

    Substring, not suffix, exactly as the reference's
    ``abstention='_abs' in entry['question_id']`` — matching on the suffix
    instead would silently grade the ``_abs`` items with the standard rubric
    (REQ-190).
    """
    return "_abs" in question_id


def judge_prompt(
    *,
    question_id: str,
    question_type: str,
    question: str,
    gold: str,
    answer: str,
) -> str:
    """Render the prompt the judge will actually send.

    ``gold`` is the dataset field verbatim. For ``single-session-preference``
    that field is a grading rubric and the selected template labels it
    ``Rubric:``; it is handed to the judge to grade against and is never
    string- or fuzzy-matched anywhere in this harness (REQ-191).
    """
    return reference_prompt(
        question_type=question_type,
        question=question,
        answer=gold,
        response=answer,
        is_abstention=is_abstention_question(question_id),
    )


def judge_session_key(question_id: str) -> str:
    """One session per graded question — no message list outlives a verdict."""
    return f"judge:{question_id}"


def parse_label(raw_response: str) -> bool:
    """Read the yes/no label back the way the reference does.

    ``'yes' in response.strip().lower()`` — permissive, and deliberately kept
    permissive: a stricter reader would score differently from the published
    numbers on exactly the borderline replies that matter.
    """
    return "yes" in raw_response.strip().lower()


# ---------------------------------------------------------------------------
# The judge agent's own config surface
# ---------------------------------------------------------------------------

_JUDGE_ARCLLM_OVERRIDES = {
    "llm": {
        "model": f'"{JUDGE_MODEL_ID}"',
        "max_tokens": str(JUDGE_MAX_TOKENS),
        "temperature": str(JUDGE_TEMPERATURE),
    },
}

JUDGE_ARCLLM_CONFIG = _apply_toml_overrides(ARCLLM_EVAL_CONFIG, _JUDGE_ARCLLM_OVERRIDES)
"""The judge's ``arcllm.toml`` — the three pinned knobs, raw body capture off."""

JUDGE_ARCRUN_CONFIG = _DEFAULT_ARCRUN_CONFIG + "allowed_tools = []  # a grader calls nothing\n"
"""The judge's ``arcrun.toml``. ``[sandbox]`` is the template's final table, so
the empty allowlist appends into it; ``[]`` is *set and empty* (none allowed),
distinct from the unset default (all allowed)."""

_JUDGE_ARCAGENT_OVERRIDES = {
    "security": {
        # Same reason as the eval agent: empty routes the WORM policy chain into
        # the shared arcstore worm dir, where it contends on one exclusive flock
        # and survives the run dir it belongs to.
        "policy_audit_log": '"audit/policy-chain.jsonl"',
    },
}


def _disable_all_modules(text: str) -> str:
    """Turn off every module the scaffold ships enabled.

    Swept rather than enumerated: a judge exists to emit one yes/no token, so
    the correct set is *none*, and an explicit list would silently readmit the
    next module that ships enabled by default. Memory matters most — a judge
    that remembered the answers it graded would carry one question's gold into
    the next question's grading.
    """
    lines = text.splitlines()
    in_module = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_module = stripped.startswith("[modules.")
            continue
        if in_module and stripped == "enabled = true":
            lines[i] = line.replace("enabled = true", "enabled = false")
    return "\n".join(lines) + "\n"


def judge_dir(run_dir: Path) -> Path:
    """The judge's config + workspace root — a sibling of the eval agent's, never it."""
    return run_dir / JUDGE_DIR_NAME


def render_judge_agent_config(*, run_dir: Path, tier: str = "personal") -> str:
    """Render the judge's ``arcagent.toml`` text — no filesystem side effects."""
    if not run_dir.is_absolute():
        raise ValueError(f"run_dir must be absolute, got {run_dir}")
    base = render_agent_config(name=JUDGE_AGENT_NAME, tier=tier)
    return _disable_all_modules(_apply_toml_overrides(base, _JUDGE_ARCAGENT_OVERRIDES))


def write_judge_agent_config(*, run_dir: Path, tier: str = "personal") -> Path:
    """Emit the judge's three sibling TOMLs plus its workspace; return the config path."""
    agent_dir = judge_dir(run_dir)
    agent_dir.mkdir(parents=True, exist_ok=True)

    config_path = agent_dir / "arcagent.toml"
    config_path.write_text(render_judge_agent_config(run_dir=run_dir, tier=tier), encoding="utf-8")
    (agent_dir / "arcllm.toml").write_text(JUDGE_ARCLLM_CONFIG, encoding="utf-8")
    (agent_dir / "arcrun.toml").write_text(JUDGE_ARCRUN_CONFIG, encoding="utf-8")

    _scaffold_workspace(agent_dir, JUDGE_AGENT_NAME)
    return config_path


def install_judge_api_key() -> str:
    """Resolve the judge's key from the environment and put it where arcllm looks.

    Environment only — never a dotenv file, never the config, never disk — so
    the key exists in exactly one place and leaves no artifact behind a run.
    The copy into ``OPENAI_API_KEY`` is the one seam we cannot avoid today:
    ``api_key_env`` is fixed by arcllm's packaged provider TOML and has no
    per-agent override, so the adapter reads that name or nothing.
    """
    key = os.environ.get(JUDGE_API_KEY_ENV, "")
    if not key:
        raise JudgeKeyMissingError(
            f"the judge needs its own API key in ${JUDGE_API_KEY_ENV} "
            "(environment only — it is never read from a file)"
        )
    os.environ[_PROVIDER_API_KEY_ENV] = key
    return key


class RunOutcome(Protocol):
    """The one field a verdict needs off a finished run."""

    content: str


class JudgeRunner(Protocol):
    """The slice of ``ArcAgent`` the judge drives.

    Narrow on purpose: it names what grading needs (one turn, one keyed
    session) and nothing else, so a test can exercise the real prompt
    selection and label parsing without an LLM call.
    """

    async def run_collected(self, input_text: str, *, session_key: str) -> Any: ...


async def build_judge_agent(*, run_dir: Path, tier: str = "personal") -> Any:
    """Build and start the judge's own ``ArcAgent`` under ``run_dir/judge``."""
    from arcagent.core.agent import ArcAgent
    from arcagent.core.config import load_config

    install_judge_api_key()
    config_path = write_judge_agent_config(run_dir=run_dir, tier=tier)
    agent = ArcAgent(load_config(config_path), config_path=config_path)
    assert_workspace_contained(workspace=agent._workspace, run_dir=judge_dir(run_dir))
    await agent.startup()
    return agent


class JudgeAgent:
    """Scores answers with a started judge agent (COMP-010)."""

    def __init__(self, runner: JudgeRunner) -> None:
        self._runner = runner

    async def judge(
        self,
        *,
        question_id: str,
        question_type: str,
        question: str,
        gold: str,
        answer: str,
    ) -> Verdict:
        """Grade one answer and return the verdict.

        ``is_abstention`` is derived from ``question_id`` rather than taken from
        the caller: it is the rule itself (REQ-190), and a bool threaded through
        four call sites is a bool one of them will eventually get wrong.
        """
        prompt = judge_prompt(
            question_id=question_id,
            question_type=question_type,
            question=question,
            gold=gold,
            answer=answer,
        )
        result = await self._runner.run_collected(
            prompt, session_key=judge_session_key(question_id)
        )
        raw_response: str = result.content
        return Verdict(
            correct=parse_label(raw_response),
            raw_response=raw_response,
            prompt_used=prompt,
            judge_model_id=JUDGE_MODEL_NAME,
        )


__all__ = [
    "JUDGE_AGENT_NAME",
    "JUDGE_API_KEY_ENV",
    "JUDGE_ARCLLM_CONFIG",
    "JUDGE_ARCRUN_CONFIG",
    "JUDGE_MAX_TOKENS",
    "JUDGE_MODEL_ID",
    "JUDGE_MODEL_NAME",
    "JUDGE_TEMPERATURE",
    "JudgeAgent",
    "JudgeKeyMissingError",
    "JudgeRunner",
    "Verdict",
    "build_judge_agent",
    "install_judge_api_key",
    "is_abstention_question",
    "judge_dir",
    "judge_prompt",
    "judge_session_key",
    "parse_label",
    "render_judge_agent_config",
    "write_judge_agent_config",
]
