"""SPEC-054 production wiring (RED) — improver-side plumbing (REQ-101/107/117).

Pins the ArcSkillImprover construction seams this spec's producers depend on:

- ``ImproverConfig.capture_args`` (default ``False``) flows into the TraceStore the
  improver constructs, together with the constructed ``tier`` — so the COMP-007
  scrub/persist decision is made arcskill-side from config, never per-call.
- ``ArcSkillImprover.observe(..., args=...)`` passes the tool-call args through to
  ``TraceStore.observe`` (the arcagent extension only forwards; REQ-117).
- DEFAULT suite trigger (COMP-004): with ``llm`` present and no ``suite_generator``
  injected, the improver constructs the production adapter over the concrete
  :class:`~arcskill.improver.suitegen.SuiteGenerator` itself (mirroring how
  ``_mutator`` defaults), so ``pip install arcskill`` + an LLM is a working
  suite-bootstrap path with zero extra wiring. No LLM -> no default trigger.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig
from arcskill.improver.config import SuiteConfig
from arcskill.improver.models import BundleView, EvalCase, EvalOutcome

_SK_SAMPLE = "sk-abc123def456ghi789jkl012"

SEED_TEXT = """\
## SKILL INTENT [IMMUTABLE]
Test skill intent.

## Steps
1. Do the thing carefully
2. Check the output
"""


def _make_skill(root: Path, name: str) -> Path:
    """A suite-less prose skill dir; returns the SKILL.md path."""
    sk = root / name
    (sk / "evals").mkdir(parents=True)
    (sk / "SKILL.md").write_text(SEED_TEXT, encoding="utf-8")
    return sk / "SKILL.md"


class _SuiteLLM:
    """Returns one non-tautological pytest candidate for the suitegen prompt."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def invoke(self, prompt: str) -> str:
        self.calls.append(prompt)
        return 'def test_anchor():\n    assert len("x") == 1\n'


class _CascadeRunner:
    """Passes every case against the real bundle, fails against the poisoned mutant."""

    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        passed = "negative-control mutant" not in view.text
        return [EvalOutcome(case_id=c.id, passed=passed) for c in cases]


class _FakeSuiteGenerator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate(self, *, skill_name: str, skill_dir: Path, kind: str) -> None:
        self.calls.append((skill_name, kind))


def _cfg(**overrides: Any) -> ImproverConfig:
    base: dict[str, Any] = {
        "optimize_after_uses": 100,
        "suite": SuiteConfig(min_cases=1, flake_runs=1),
    }
    base.update(overrides)
    return ImproverConfig(**base)


async def _use(imp: ArcSkillImprover, skill: str, *, args: dict[str, Any] | None = None) -> None:
    await imp.observe(skill_name=skill, tool_name="run", status="ok", error_type=None, args=args)
    await imp.on_turn_end(turn=0, outcome="")


def _persisted_call(workspace: Path, skill: str) -> dict[str, Any]:
    files = list((workspace / "skill_traces" / skill).glob("traces-*.jsonl"))
    assert len(files) == 1
    record: dict[str, Any] = json.loads(files[0].read_text(encoding="utf-8").strip())
    calls: list[dict[str, Any]] = record["tool_calls"]
    assert len(calls) == 1
    return calls[0]


# -- COMP-004: default production suite trigger --------------------------------


@pytest.mark.asyncio
async def test_default_suite_trigger_constructed_from_llm_and_generates(tmp_path: Path) -> None:
    """No suite_generator injected + llm present: the sweep drives the concrete
    SuiteGenerator through the default adapter and adopts anchors on disk."""
    skill_md = _make_skill(tmp_path, "s")
    llm = _SuiteLLM()
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=_cfg(),
        tier="personal",
        llm=llm,
        eval_runner=_CascadeRunner(),
        skill_path=lambda name: skill_md,
    )
    await _use(imp, "s")
    await asyncio.wait_for(imp.sweep_suites(), timeout=5)
    await imp.aclose()

    generated = skill_md.parent / "evals" / "test_golden_generated.py"
    assert generated.exists(), "default trigger never drove the concrete SuiteGenerator"
    assert "def test_anchor" in generated.read_text(encoding="utf-8")
    assert any("golden" in call for call in llm.calls)


@pytest.mark.asyncio
async def test_no_default_trigger_without_llm(tmp_path: Path) -> None:
    """No llm and no injected generator: the sweep stays a silent no-op (no files)."""
    skill_md = _make_skill(tmp_path, "s")
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=_cfg(),
        tier="personal",
        eval_runner=_CascadeRunner(),
        skill_path=lambda name: skill_md,
    )
    await _use(imp, "s")
    await asyncio.wait_for(imp.sweep_suites(), timeout=5)
    await imp.aclose()

    assert not (skill_md.parent / "evals" / "test_golden_generated.py").exists()


@pytest.mark.asyncio
async def test_injected_suite_generator_wins_over_default(tmp_path: Path) -> None:
    skill_md = _make_skill(tmp_path, "s")
    gen = _FakeSuiteGenerator()
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=_cfg(),
        tier="personal",
        llm=_SuiteLLM(),
        eval_runner=_CascadeRunner(),
        suite_generator=gen,
        skill_path=lambda name: skill_md,
    )
    await _use(imp, "s")
    await asyncio.wait_for(imp.sweep_suites(), timeout=5)
    await imp.aclose()

    assert gen.calls == [("s", "create")]
    assert not (skill_md.parent / "evals" / "test_golden_generated.py").exists()


# -- COMP-007: capture_args + tier flow into the TraceStore ---------------------


@pytest.mark.asyncio
async def test_observe_args_passthrough_default_off_hashes_only(tmp_path: Path) -> None:
    """Default capture off: passed args produce an args_hash but never persist."""
    ws = tmp_path / "ws"
    imp = ArcSkillImprover(ws, config=ImproverConfig(), tier="personal")
    await _use(imp, "s", args={"query": "weather", "api_key": _SK_SAMPLE})
    await imp.aclose()

    call = _persisted_call(ws, "s")
    assert call["args_hash"] != "", "args never reached the TraceStore"
    assert call.get("args") is None


@pytest.mark.asyncio
async def test_capture_args_config_flows_to_trace_store(tmp_path: Path) -> None:
    """capture_args=True in ImproverConfig: scrubbed args persist to the JSONL."""
    ws = tmp_path / "ws"
    imp = ArcSkillImprover(ws, config=ImproverConfig(capture_args=True), tier="personal")
    await _use(imp, "s", args={"query": "weather", "api_key": _SK_SAMPLE})
    await imp.aclose()

    call = _persisted_call(ws, "s")
    assert call["args"] is not None
    assert call["args"]["query"] == "weather"
    assert _SK_SAMPLE not in json.dumps(call["args"])


@pytest.mark.asyncio
async def test_tier_flows_to_trace_store_federal_stays_hash_only(tmp_path: Path) -> None:
    """The constructed tier reaches the TraceStore: federal ignores the capture knob."""
    ws = tmp_path / "ws"
    imp = ArcSkillImprover(ws, config=ImproverConfig(capture_args=True), tier="federal")
    await _use(imp, "s", args={"query": "weather"})
    await imp.aclose()

    call = _persisted_call(ws, "s")
    assert call.get("args") is None
    assert call["args_hash"] != ""
