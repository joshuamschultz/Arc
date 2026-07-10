"""SPEC-054 COMP-001 — SuiteGenerator adoption cascade (REQ-101/102/103/104).

Candidate golden cases come back from ONE bounded LLMInvoker call as a pytest module
source; each top-level ``def test_*`` function is one candidate. Candidates walk the
ordered cascade — per-candidate ``ast.parse`` (a broken candidate never kills its
siblings), anti-tautology static reject (assert-free, ``assert True``-class, and
input-literal-only bodies), then ``flake_runs`` sandboxed executions against the
CURRENT bundle, then a negative-control mutation probe against a deliberately mutated
bundle. Only all-stage passers are adopted as regression anchors. Adoption writes
``evals/test_golden_generated.py`` atomically with the ``@generated`` module docstring
marker plus a harness-written ``evals/.manifest.json`` sha256 entry (add-only next to
human files). Failing candidates quarantine as improvement targets and NEVER enter the
adopted suite. Generation stops once ``min_cases`` anchors are adopted and never
examines more than ``candidate_budget`` candidates (LLM10).
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest
from arcskill.improver.config import SuiteConfig
from arcskill.improver.evalgate import load_suite
from arcskill.improver.models import BundleView, EvalCase, EvalOutcome
from arcskill.improver.suitegen import GenerationResult, QuarantinedCase, SuiteGenerator

# -- skill fixture: SKILL.md with the oracle-grounding sections (REQ-102) ------

_CONTRACT = "add(a, b) returns the exact integer sum of a and b."
_EXAMPLES = "add(2, 3) == 5; add(-1, 1) == 0"
_VALIDATION = "Every arithmetic claim must be backed by an executable assertion."

_SKILL_MD = f"""# calc

## Contract

{_CONTRACT}

## Examples

{_EXAMPLES}

## Validation

{_VALIDATION}
"""


def _skill(tmp_path: Path) -> BundleView:
    skill_dir = tmp_path / "calc"
    (skill_dir / "evals").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    return BundleView(
        skill_name="calc",
        text=_SKILL_MD,
        skill_dir=skill_dir,
        scripts={"scripts/calc.py": b"def add(a, b):\n    return a + b\n"},
    )


# -- scripted candidate sources -------------------------------------------------


def _candidate(name: str) -> str:
    """A well-formed, non-tautological candidate: asserts on a derived call result."""
    return f"def {name}():\n    from scripts.calc import add\n    assert add(2, 3) == 5\n"


def _module(*funcs: str) -> str:
    return "\n\n".join(funcs) + "\n"


_GOOD_CANDIDATE = _candidate("test_add_contract")

_BROKEN_CANDIDATE = "def test_broken():\n    assert add(2, 3 ==\n"

_TAUT_ASSERT_FREE = "def test_assert_free():\n    from scripts.calc import add\n    add(1, 2)\n"
_TAUT_ASSERT_TRUE = "def test_assert_true():\n    assert True\n"
_TAUT_CONSTANT_COMPARE = "def test_constant_compare():\n    assert 1 == 1\n"
_TAUT_INPUT_LITERAL = 'def test_input_literal():\n    payload = "42"\n    assert payload == "42"\n'


# -- deterministic seam fakes ----------------------------------------------------


class _ScriptedLLM:
    """LLMInvoker fake: returns one scripted pytest module source, records prompts."""

    def __init__(self, response: str) -> None:
        self.prompts: list[str] = []
        self._response = response

    async def invoke(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._response


_POISON = b"negative-control mutant"


def _is_poisoned(view: BundleView) -> bool:
    """True when the mutant probe's poison marker is present (scripts or prose)."""
    scripts = {p: d for p, d in view.scripts.items() if not p.startswith("evals/")}
    if any(_POISON in data for data in scripts.values()):
        return True
    return _POISON.decode("utf-8") in view.text


class _RecordingRunner:
    """Deterministic EvalRunner: scripts per-case outcomes and records every call.

    ``current`` maps a candidate function name to its pass/fail sequence across
    successive current-bundle runs (last value repeats; default all-pass).
    ``mutant_pass`` scripts the negative-control probe; the default ``False`` means
    the case FAILS on the mutant — it discriminates, so probe-agnostic tests adopt
    cleanly. A call's bundle is classified by the poison marker (the mutant probe
    appends ``_MUTANT_POISON`` to the original bundle): poisoned → "mutant", else
    "current". Every run must also carry the candidate's MATERIALIZED source in the
    scripts overlay — an in-memory-only case can never pass a real sandbox (the
    SPEC-054 E2E producers-unwired gap this fake previously hid).
    """

    def __init__(
        self,
        current_view: BundleView,
        current: dict[str, list[bool]] | None = None,
        mutant_pass: dict[str, bool] | None = None,
    ) -> None:
        self._current_view = current_view
        self._current = current or {}
        self._mutant_pass = mutant_pass or {}
        self._seen: dict[str, int] = {}
        self.calls: list[tuple[str, list[str]]] = []

    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        kind = "mutant" if _is_poisoned(view) else "current"
        names = [c.id.split("::")[-1] for c in cases]
        # Materialization invariant: the candidate under test must exist as a real
        # file in the bundle the runner is asked to execute.
        generated = view.scripts.get("evals/test_golden_generated.py", b"").decode("utf-8")
        for name in names:
            assert f"def {name}(" in generated, (
                f"candidate {name} not materialized into the run bundle"
            )
        self.calls.append((kind, names))
        return [
            EvalOutcome(case_id=case.id, passed=self._passed(kind, name))
            for case, name in zip(cases, names, strict=True)
        ]

    def _passed(self, kind: str, name: str) -> bool:
        if kind == "mutant":
            return self._mutant_pass.get(name, False)
        seq = self._current.get(name, [True])
        i = self._seen.get(name, 0)
        self._seen[name] = i + 1
        return seq[min(i, len(seq) - 1)]

    def current_calls_for(self, name: str) -> int:
        return sum(1 for kind, names in self.calls if kind == "current" and name in names)

    def ever_saw(self, name: str) -> bool:
        return any(name in names for _, names in self.calls)


def _generator(llm: _ScriptedLLM, runner: _RecordingRunner, **config_over: int) -> SuiteGenerator:
    return SuiteGenerator(llm=llm, runner=runner, config=SuiteConfig(flake_runs=3, **config_over))


def _adopted_names(result: GenerationResult) -> list[str]:
    return [c.id.split("::")[-1] for c in result.adopted]


# -- ordered cascade: static rejects run before the sandbox (REQ-102) ------------


@pytest.mark.asyncio
async def test_syntax_error_candidate_discarded_with_zero_runner_calls(tmp_path: Path) -> None:
    view = _skill(tmp_path)
    llm = _ScriptedLLM(_module(_BROKEN_CANDIDATE, _GOOD_CANDIDATE))
    runner = _RecordingRunner(view)

    result = await _generator(llm, runner).generate("calc", view)

    assert isinstance(result, GenerationResult)
    assert result.discarded == 1
    assert not runner.ever_saw("test_broken")
    assert _adopted_names(result) == ["test_add_contract"]


@pytest.mark.asyncio
async def test_tautology_candidates_discarded_before_any_runner_call(tmp_path: Path) -> None:
    view = _skill(tmp_path)
    tautologies = (
        _TAUT_ASSERT_FREE,
        _TAUT_ASSERT_TRUE,
        _TAUT_CONSTANT_COMPARE,
        _TAUT_INPUT_LITERAL,
    )
    llm = _ScriptedLLM(_module(*tautologies, _GOOD_CANDIDATE))
    runner = _RecordingRunner(view)

    result = await _generator(llm, runner).generate("calc", view)

    assert result.discarded == 4
    for name in (
        "test_assert_free",
        "test_assert_true",
        "test_constant_compare",
        "test_input_literal",
    ):
        assert not runner.ever_saw(name)
    assert _adopted_names(result) == ["test_add_contract"]


# -- flake gate: N current-bundle runs decide adopt vs flaky (REQ-102) -----------


@pytest.mark.asyncio
async def test_all_pass_candidate_is_adopted_after_flake_runs_executions(
    tmp_path: Path,
) -> None:
    view = _skill(tmp_path)
    runner = _RecordingRunner(view)

    result = await _generator(_ScriptedLLM(_module(_GOOD_CANDIDATE)), runner).generate(
        "calc", view
    )

    assert _adopted_names(result) == ["test_add_contract"]
    assert all(c.machine_authored for c in result.adopted)
    assert result.quarantined == []
    # exactly flake_runs (=3, non-default to prove config is honored) current-bundle runs
    assert runner.current_calls_for("test_add_contract") == 3


@pytest.mark.asyncio
async def test_mixed_pass_fail_candidate_quarantined_as_flaky(tmp_path: Path) -> None:
    view = _skill(tmp_path)
    runner = _RecordingRunner(view, current={"test_add_contract": [True, False, True]})

    result = await _generator(_ScriptedLLM(_module(_GOOD_CANDIDATE)), runner).generate(
        "calc", view
    )

    assert result.adopted == []
    assert len(result.quarantined) == 1
    quarantined = result.quarantined[0]
    assert isinstance(quarantined, QuarantinedCase)
    assert quarantined.nodeid.endswith("test_add_contract")
    assert "flaky" in quarantined.reason.lower()


# -- negative-control mutation probe (REQ-104) -----------------------------------


@pytest.mark.asyncio
async def test_candidate_passing_mutated_bundle_quarantined_by_probe(tmp_path: Path) -> None:
    """A case that also passes against a deliberately mutated bundle discriminates
    nothing — quarantine, never adopt."""
    view = _skill(tmp_path)
    runner = _RecordingRunner(view, mutant_pass={"test_add_contract": True})

    result = await _generator(_ScriptedLLM(_module(_GOOD_CANDIDATE)), runner).generate(
        "calc", view
    )

    assert result.adopted == []
    assert len(result.quarantined) == 1
    assert result.quarantined[0].nodeid.endswith("test_add_contract")
    assert "mutation" in result.quarantined[0].reason.lower()
    # the generator actually invoked the probe against a bundle != the current one
    assert any(kind == "mutant" and "test_add_contract" in names for kind, names in runner.calls)


# -- failing candidates are improvement targets, never anchors (REQ-103) ---------


@pytest.mark.asyncio
async def test_failing_candidate_quarantined_as_improvement_target_never_written(
    tmp_path: Path,
) -> None:
    view = _skill(tmp_path)
    source = _module(_candidate("test_wrong_expectation"), _GOOD_CANDIDATE)
    runner = _RecordingRunner(view, current={"test_wrong_expectation": [False]})

    result = await _generator(_ScriptedLLM(source), runner, min_cases=1).generate("calc", view)

    assert _adopted_names(result) == ["test_add_contract"]
    assert len(result.quarantined) == 1
    quarantined = result.quarantined[0]
    assert quarantined.nodeid.endswith("test_wrong_expectation")
    assert "improvement" in quarantined.reason.lower()
    assert view.skill_dir is not None
    generated = view.skill_dir / "evals" / "test_golden_generated.py"
    text = generated.read_text(encoding="utf-8")
    assert "test_add_contract" in text
    assert "test_wrong_expectation" not in text


# -- adoption write: marker + manifest provenance, add-only (REQ-102/109) --------


@pytest.mark.asyncio
async def test_adoption_writes_generated_file_with_marker_and_manifest(
    tmp_path: Path,
) -> None:
    view = _skill(tmp_path)
    names = ["test_a", "test_b", "test_c"]
    llm = _ScriptedLLM(_module(*[_candidate(n) for n in names]))

    result = await _generator(llm, _RecordingRunner(view), min_cases=3).generate("calc", view)

    assert view.skill_dir is not None
    evals = view.skill_dir / "evals"
    generated = evals / "test_golden_generated.py"
    tree = ast.parse(generated.read_text(encoding="utf-8"))
    assert "@generated" in (ast.get_docstring(tree) or "")

    manifest = json.loads((evals / ".manifest.json").read_text(encoding="utf-8"))
    recorded = manifest["files"]["test_golden_generated.py"]["sha256"]
    assert recorded == hashlib.sha256(generated.read_bytes()).hexdigest()

    # load_suite (COMP-002) classifies the adopted anchors machine-authored
    cases = load_suite(view.skill_dir)
    assert {c.node for c in cases} == {f"evals/test_golden_generated.py::{name}" for name in names}
    assert all(c.machine_authored for c in cases)
    assert sorted(c.node for c in result.adopted) == sorted(c.node for c in cases)


@pytest.mark.asyncio
async def test_existing_human_eval_files_untouched_add_only(tmp_path: Path) -> None:
    view = _skill(tmp_path)
    assert view.skill_dir is not None
    human = view.skill_dir / "evals" / "test_human.py"
    human_body = "def test_human():\n    assert (2 * 2) == 4\n"
    human.write_text(human_body, encoding="utf-8")
    names = ["test_a", "test_b", "test_c"]
    llm = _ScriptedLLM(_module(*[_candidate(n) for n in names]))

    await _generator(llm, _RecordingRunner(view), min_cases=3).generate("calc", view)

    assert human.read_text(encoding="utf-8") == human_body
    # add-only, atomic: exactly the human file, the generated file, and the manifest —
    # no temp-file residue in evals/
    assert sorted(p.name for p in (view.skill_dir / "evals").iterdir()) == [
        ".manifest.json",
        "test_golden_generated.py",
        "test_human.py",
    ]
    by_node = {c.node: c for c in load_suite(view.skill_dir)}
    assert by_node["evals/test_human.py::test_human"].machine_authored is False


# -- budgets: early stop at min_cases, hard cap at candidate_budget (LLM10) ------


@pytest.mark.asyncio
async def test_generation_stops_once_min_cases_anchors_adopted(tmp_path: Path) -> None:
    view = _skill(tmp_path)
    names = [f"test_c{i}" for i in range(1, 7)]
    runner = _RecordingRunner(view)
    llm = _ScriptedLLM(_module(*[_candidate(n) for n in names]))

    result = await _generator(llm, runner, min_cases=3).generate("calc", view)

    assert _adopted_names(result) == names[:3]
    for name in names[3:]:
        assert not runner.ever_saw(name)


@pytest.mark.asyncio
async def test_candidates_examined_never_exceed_candidate_budget(tmp_path: Path) -> None:
    view = _skill(tmp_path)
    names = [f"test_c{i}" for i in range(1, 7)]
    runner = _RecordingRunner(view, current={name: [False] for name in names})
    llm = _ScriptedLLM(_module(*[_candidate(n) for n in names]))

    result = await _generator(llm, runner, candidate_budget=4).generate("calc", view)

    assert result.adopted == []
    assert sorted(q.nodeid.split("::")[-1] for q in result.quarantined) == sorted(names[:4])
    for name in names[4:]:
        assert not runner.ever_saw(name)


# -- oracle grounding: prompt carries the declared sections (REQ-102, anti-bug-freezing)


@pytest.mark.asyncio
async def test_generation_prompt_grounds_in_contract_examples_validation(
    tmp_path: Path,
) -> None:
    view = _skill(tmp_path)
    llm = _ScriptedLLM(_module(_GOOD_CANDIDATE))

    await _generator(llm, _RecordingRunner(view)).generate("calc", view)

    assert len(llm.prompts) == 1  # one bounded generation call (COMP-001)
    prompt = llm.prompts[0]
    assert _CONTRACT in prompt
    assert _EXAMPLES in prompt
    assert _VALIDATION in prompt
