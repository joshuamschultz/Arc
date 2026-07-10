"""Golden-suite bootstrap — the SuiteGenerator adoption cascade (SPEC-054 COMP-001).

One bounded :class:`~arcskill.improver.seams.LLMInvoker` call proposes a pytest module
whose top-level ``def test_*`` functions are candidate golden cases, oracle-grounded in
the skill's declared Contract/Examples/Validation prose (REQ-102, anti-bug-freezing).
Each candidate walks an ordered cascade: per-candidate ``ast.parse`` (a broken candidate
never kills its siblings), anti-tautology static reject, ``flake_runs`` sandboxed
executions against the current bundle, then a negative-control mutation probe against a
deliberately broken bundle (REQ-104). Only all-stage passers are adopted; failures
quarantine as improvement targets (REQ-103) and never enter the suite. Generation stops
at ``min_cases`` adopted anchors and never examines more than ``candidate_budget``
candidates (LLM10).

Adoption writes ``evals/test_golden_generated.py`` (module docstring carries the
``@generated`` marker) plus the harness manifest entry in ``evals/.manifest.json`` —
both atomically via temp-file + ``os.replace``, add-only beside human eval files, so
:func:`~arcskill.improver.evalgate.load_suite` classifies the anchors machine-authored
(REQ-109).
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from arcskill.improver.config import SuiteConfig
from arcskill.improver.models import BundleView, EvalCase
from arcskill.improver.seams import EvalRunner, LLMInvoker

_GENERATED_NAME = "test_golden_generated.py"
_GENERATED_DOCSTRING = (
    '"""@generated golden anchors — machine-authored by arcskill.improver.suitegen."""'
)
# The negative control must change *behavior*, not just bytes: an import-time raise
# guarantees any case that still passes discriminates nothing about the bundle.
_MUTANT_POISON = b"\nraise RuntimeError('arcskill suitegen negative-control mutant')\n"


@dataclass(frozen=True)
class QuarantinedCase:
    """A candidate that failed the cascade: an improvement target, never an anchor."""

    nodeid: str
    reason: str


@dataclass(frozen=True)
class GenerationResult:
    """Outcome of one generation run: adopted anchors, quarantine, static discards."""

    adopted: list[EvalCase]
    quarantined: list[QuarantinedCase]
    discarded: int


class SuiteGenerator:
    """Bootstraps a skill's golden suite through the adoption cascade."""

    def __init__(self, *, llm: LLMInvoker, runner: EvalRunner, config: SuiteConfig) -> None:
        self._llm = llm
        self._runner = runner
        self._config = config

    async def generate(self, skill_name: str, view: BundleView) -> GenerationResult:
        """Generate, vet, and adopt golden cases for ``skill_name`` over ``view``."""
        source = await self._llm.invoke(self._prompt(skill_name, view))
        adopted: list[EvalCase] = []
        adopted_sources: list[str] = []
        quarantined: list[QuarantinedCase] = []
        discarded = 0
        for candidate in _split_candidates(source)[: self._config.candidate_budget]:
            if len(adopted) >= self._config.min_cases:
                break
            func = _parse_candidate(candidate)
            if func is None or _is_tautology(func):
                discarded += 1
                continue
            nodeid = f"evals/{_GENERATED_NAME}::{func.name}"
            case = EvalCase(id=nodeid, node=nodeid, machine_authored=True)
            reason = await self._cascade_verdict(view, case, candidate)
            if reason is None:
                adopted.append(case)
                adopted_sources.append(candidate)
            else:
                quarantined.append(QuarantinedCase(nodeid=nodeid, reason=reason))
        if adopted and view.skill_dir is not None:
            _write_adopted(view.skill_dir, adopted_sources)
        return GenerationResult(adopted=adopted, quarantined=quarantined, discarded=discarded)

    def _prompt(self, skill_name: str, view: BundleView) -> str:
        return (
            f"Generate pytest golden regression cases for the skill '{skill_name}'.\n"
            "Ground every assertion oracle ONLY in the declared Contract, Examples, and\n"
            "Validation sections below — never in observed behavior, so current bugs are\n"
            "not frozen in as expectations.\n\n"
            f"{view.text}\n\n"
            f"Return one Python module containing at most {self._config.max_cases}\n"
            "top-level `def test_*` functions and nothing else."
        )

    async def _cascade_verdict(self, view: BundleView, case: EvalCase, source: str) -> str | None:
        """Run the sandbox stages; ``None`` means adopt, otherwise the quarantine reason.

        The candidate exists only as in-memory source until adoption, so every run
        materializes it into the bundle via the scripts overlay — the runner builds
        the sandbox from the view, and a case with no file can never pass (the
        SPEC-054 E2E producers-unwired gap). The mutant probe poisons the ORIGINAL
        bundle first, then overlays the candidate, so the probe never poisons the
        case it is probing.
        """
        run_view = _with_candidate(view, source)
        results = [await self._run_once(run_view, case) for _ in range(self._config.flake_runs)]
        if not any(results):
            return "improvement target: fails the current bundle on every run"
        if not all(results):
            return "flaky: mixed pass/fail across flake runs"
        if await self._run_once(_with_candidate(_mutated(view), source), case):
            return "mutation probe survived: also passes a deliberately mutated bundle"
        return None

    async def _run_once(self, view: BundleView, case: EvalCase) -> bool:
        outcomes = await self._runner.run(view, [case])
        return all(outcome.passed for outcome in outcomes)


def _split_candidates(source: str) -> list[str]:
    """Split module source into per-candidate chunks at each top-level ``def test_*``."""
    blocks: list[list[str]] = []
    for line in source.splitlines():
        if line.startswith("def test_"):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    return ["\n".join(block).rstrip() + "\n" for block in blocks]


def _parse_candidate(source: str) -> ast.FunctionDef | None:
    """Parse one candidate in isolation; ``None`` discards it without killing siblings."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    funcs = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    return funcs[0] if len(funcs) == 1 else None


def _is_tautology(func: ast.FunctionDef) -> bool:
    """True unless some assert exercises a call — assert-free bodies, ``assert True``,
    constant compares, and input-literal echoes all pass vacuously (REQ-102)."""
    asserts = [node for node in ast.walk(func) if isinstance(node, ast.Assert)]
    return not any(isinstance(node, ast.Call) for stmt in asserts for node in ast.walk(stmt.test))


def _with_candidate(view: BundleView, source: str) -> BundleView:
    """Overlay the candidate file into the sandbox bundle at its future adopted path."""
    scripts = {**view.scripts, f"evals/{_GENERATED_NAME}": source.encode("utf-8")}
    return replace(view, scripts=scripts)


def _mutated(view: BundleView) -> BundleView:
    """Negative-control bundle (REQ-104): poison the scripts (or prose) so a
    discriminating case must fail against it."""
    if view.scripts:
        poisoned = {path: data + _MUTANT_POISON for path, data in view.scripts.items()}
        return replace(view, scripts=poisoned)
    return replace(view, text=view.text + _MUTANT_POISON.decode("utf-8"))


def _write_adopted(skill_dir: Path, sources: list[str]) -> None:
    """Write the generated suite + manifest hash of the FINAL bytes, atomically, add-only."""
    evals_dir = skill_dir / "evals"
    evals_dir.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join(source.rstrip("\n") for source in sources)
    content = f"{_GENERATED_DOCSTRING}\n\n{body}\n".encode()
    _atomic_write(evals_dir / _GENERATED_NAME, content)
    manifest = _read_manifest(evals_dir)
    manifest["files"][_GENERATED_NAME] = {"sha256": hashlib.sha256(content).hexdigest()}
    _atomic_write(evals_dir / ".manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))


def _read_manifest(evals_dir: Path) -> dict[str, Any]:
    """Load the existing manifest so adoption merges add-only instead of clobbering."""
    try:
        raw = json.loads((evals_dir / ".manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"files": {}}
    if isinstance(raw, dict) and isinstance(raw.get("files"), dict):
        return raw
    return {"files": {}}


def _atomic_write(target: Path, data: bytes) -> None:
    """Temp-file + ``os.replace`` (lock.py pattern) so a crash never leaves a partial
    file, and the temp never lingers beside human eval files."""
    fd, tmp_path = tempfile.mkstemp(dir=target.parent, prefix=".suitegen-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, target)
    finally:
        # After a successful replace the temp path is already gone; on any failure
        # this removes the residue the add-only invariant forbids.
        Path(tmp_path).unlink(missing_ok=True)


__all__ = ["GenerationResult", "QuarantinedCase", "SuiteGenerator"]
