"""Integration: an operator's signed overlay for an arcskill improver prompt takes
effect at runtime through the agent's ``PromptSource`` (editable-system-prompts, COMP-030).

arcagent builds the overlay-aware resolver and HANDS arcskill a ``PromptSource`` over it.
This proves the whole seam end-to-end: a signed operator overlay under
``<agent_root>/context/arcskill/<name>.md`` (+ ``.arcsig``) overrides the stock prompt
when an arcskill improver component is handed the agent's source, and the same component
falls back to shipped stock when handed ``prompt_source=None``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import arctrust
from arcprompt import PromptSource, ResolverPromptSource, render_prompt
from arcskill.improver.config import ImproverConfig
from arcskill.improver.evaluator import SkillEvaluator
from arcskill.improver.models import SkillTrace
from arcskill.improver.mutate import SkillReflector
from arctrust.artifact import sign_artifact
from arctrust.operator import OperatorKey
from arctrust.policy import OperatorApprovalAuthority

from arcagent.core.prompt_context import build_prompt_resolver


class _DummyLLM:
    """The judge/reflector LLM seam — never invoked by prompt-building here."""

    async def invoke(self, prompt: str) -> str:  # pragma: no cover - unused
        return ""


def _operator(tmp_path: Path, monkeypatch: Any) -> tuple[OperatorKey, str]:
    """Create a deployment operator key at a temp path and pin it as the default."""
    key_path = tmp_path / "operator" / "operator.key"
    key_path.parent.mkdir(parents=True)
    op = OperatorKey.generate()
    op.save(key_path)
    monkeypatch.setattr(arctrust, "default_operator_key_path", lambda: key_path)
    monkeypatch.setattr(
        "arctrust.operator_resolver.default_operator_key_path", lambda base=None: key_path
    )
    did = OperatorApprovalAuthority(op.into_signer()).did
    return op, did


def _sign_overlay(agent_root: Path, name: str, body: str, op: OperatorKey, did: str) -> None:
    """Author + operator-sign an arcskill prompt overlay as the arcui SigningAuthority does."""
    overlay_dir = agent_root / "context" / "arcskill"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    raw = render_prompt(body, name=name, description="operator override")
    (overlay_dir / f"{name}.md").write_bytes(raw)
    manifest = sign_artifact(raw, signer_did=did, private_key=op.seed)
    (overlay_dir / f"{name}.md.arcsig").write_text(manifest.to_json(), encoding="utf-8")


def _agent_source(agent_root: Path, tier: str) -> PromptSource:
    """The source the agent hands arcskill: its resolver rooted at the agent folder."""
    return ResolverPromptSource(build_prompt_resolver(agent_root / "arcagent.toml", tier))


def _agent_root(tmp_path: Path) -> Path:
    agent_root = tmp_path / "team" / "an_agent"
    (agent_root / "workspace").mkdir(parents=True)
    return agent_root


def test_reflection_prompt_overlay_overrides_stock(tmp_path: Path, monkeypatch: Any) -> None:
    """SkillReflector (Part 1 wiring): a signed reflection_prompt overlay wins."""
    op, did = _operator(tmp_path, monkeypatch)
    agent_root = _agent_root(tmp_path)
    _sign_overlay(agent_root, "reflection_prompt", "OVERRIDDEN REFLECT {dims_text}", op, did)

    source = _agent_source(agent_root, "federal")
    overlaid = SkillReflector(ImproverConfig(), _DummyLLM(), prompt_source=source)
    prompt = overlaid.build_reflection_prompt("current skill", ["accuracy"], [], 1000)
    assert prompt == "OVERRIDDEN REFLECT accuracy"

    # prompt_source=None -> shipped stock, no override.
    stock = SkillReflector(ImproverConfig(), _DummyLLM(), prompt_source=None)
    stock_prompt = stock.build_reflection_prompt("current skill", ["accuracy"], [], 1000)
    assert "OVERRIDDEN REFLECT" not in stock_prompt
    assert "You are improving a skill procedure document." in stock_prompt


def test_judge_prompt_overlay_overrides_stock(tmp_path: Path, monkeypatch: Any) -> None:
    """SkillEvaluator (already wired): a signed judge_prompt overlay wins; rubric stays stock."""
    op, did = _operator(tmp_path, monkeypatch)
    agent_root = _agent_root(tmp_path)
    _sign_overlay(agent_root, "judge_prompt", "OVERRIDDEN JUDGE {dimension} {skill_text}", op, did)

    trace = SkillTrace(
        trace_id="t1",
        session_id="s1",
        skill_name="demo",
        skill_version=0,
        turn_number=1,
        started_at=datetime.now(UTC),
    )
    source = _agent_source(agent_root, "federal")
    overlaid = SkillEvaluator(ImproverConfig(), _DummyLLM(), prompt_source=source)
    prompt = overlaid.build_judge_prompt("SKILLBODY", trace, "accuracy")
    assert prompt == "OVERRIDDEN JUDGE accuracy SKILLBODY"

    # prompt_source=None -> shipped stock, no override.
    stock = SkillEvaluator(ImproverConfig(), _DummyLLM(), prompt_source=None)
    stock_prompt = stock.build_judge_prompt("SKILLBODY", trace, "accuracy")
    assert "OVERRIDDEN JUDGE" not in stock_prompt
