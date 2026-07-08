"""Safety guardrails for skill optimization.

Pre-optimization eligibility checks and post-mutation candidate validation.
Prevents catastrophic regression, semantic drift, length explosion,
feedback loops, and unauthorized optimization of security-critical skills.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from arcskill.improver.config import ChangeBoundConfig, ImproverConfig
from arcskill.improver.models import BundlePatch, Candidate, SkillTrace

# Regex to extract the immutable intent block
_INTENT_RE = re.compile(
    r"##\s+SKILL INTENT\s+\[IMMUTABLE\]\s*\n(.*?)(?=\n##|\Z)",
    re.DOTALL,
)


@dataclass(frozen=True)
class BoundLimits:
    """Resolved change-bound limits for one tier (SPEC-044 §7, SkillOpt-pinned)."""

    max_edits: int  # SkillOpt Lt — edit operations (hunks) per step ("textual learning rate")
    edit_schedule: str  # constant | linear | cosine
    min_edits_floor: int  # cosine never decays below this
    max_files_touched: int
    max_lines_changed: int
    max_ast_distance: float  # convergence regularizer, NOT a security gate (§8)
    max_prose_edit_distance: float


# PINNED per-tier defaults (SDD §7 / OQ-1). Federal is the tightest and non-relaxable:
# the resolver only ever tightens via min(), so config/skill overrides cannot loosen it.
TIER_BOUNDS: dict[str, BoundLimits] = {
    "personal": BoundLimits(8, "constant", 2, 3, 80, 0.0, 0.25),
    "enterprise": BoundLimits(4, "cosine", 2, 2, 40, 0.30, 0.15),
    "federal": BoundLimits(2, "cosine", 2, 1, 15, 0.20, 0.15),
}


class ChangeBound:
    """SkillOpt bounded-edit gate over a :class:`BundlePatch` (REQ-030/031).

    Runs **before** the (expensive, sandboxed) golden-task eval so an over-budget patch
    is rejected pre-eval and audited (AC-4). Tier flows through construction — the tier
    ceiling is bound here, config/skill overrides may only tighten it (federal floor
    non-relaxable). ``max_ast_distance`` is a convergence regularizer, never a security
    boundary — the sandbox + re-sign + re-verify chain contains a malicious patch (§8).
    """

    def __init__(self, tier: str, override: ChangeBoundConfig | None = None) -> None:
        base = TIER_BOUNDS.get(tier, TIER_BOUNDS["personal"])
        self._tier = tier
        self._limits = _resolve(base, override)

    @property
    def limits(self) -> BoundLimits:
        return self._limits

    def scheduled_edits(self, attempt: int, total_attempts: int) -> int:
        """Edit budget for ``attempt`` under the schedule (cosine decays ceiling→floor)."""
        ceil, floor = self._limits.max_edits, self._limits.min_edits_floor
        if self._limits.edit_schedule != "cosine" or total_attempts <= 1 or ceil <= floor:
            return ceil
        frac = min(max(attempt, 0), total_attempts - 1) / (total_attempts - 1)
        decayed = floor + (ceil - floor) * (1 + math.cos(math.pi * frac)) / 2
        return max(floor, round(decayed))

    def check(
        self,
        patch: BundlePatch,
        base_scripts: dict[str, bytes],
        *,
        skill_override: ChangeBoundConfig | None = None,
        edit_budget: int | None = None,
    ) -> tuple[bool, str]:
        """Return ``(ok, reason)``: reject if the patch exceeds any resolved bound."""
        limits = _resolve(self._limits, skill_override) if skill_override else self._limits
        if patch.files_touched > limits.max_files_touched:
            return False, f"files_touched {patch.files_touched} > max {limits.max_files_touched}"
        lines = patch.lines_changed(base_scripts)
        if lines > limits.max_lines_changed:
            return False, f"lines_changed {lines} > max {limits.max_lines_changed}"
        edits = _edit_ops(patch, base_scripts)
        cap = limits.max_edits if edit_budget is None else min(limits.max_edits, edit_budget)
        if edits > cap:
            return False, f"edit_ops {edits} > max_edits {cap} (SkillOpt Lt)"
        dist = _ast_distance(patch, base_scripts)
        if limits.max_ast_distance > 0.0 and dist > limits.max_ast_distance:
            return False, f"ast_distance {dist:.2f} > max {limits.max_ast_distance} (regularizer)"
        return True, "within change bound"


class Guardrails:
    """Safety checks for skill optimization eligibility and candidate validation."""

    def __init__(self, config: ImproverConfig) -> None:
        self._config = config
        self._cooloffs: dict[str, int] = {}  # skill_name -> until_turn
        self._generations: dict[str, int] = {}  # skill_name -> current generation

    def check_eligible(
        self,
        skill_name: str,
        traces: list[SkillTrace],
        *,
        current_turn: int = 0,
        skill_tags: list[str] | None = None,
    ) -> bool:
        """Pre-optimization checks: min traces, cooloff, exempt tags, generation limit."""
        if len(traces) < self._config.min_traces:
            return False
        if self.in_cooloff(skill_name, current_turn):
            return False
        if skill_tags and self._is_exempt(skill_tags):
            return False
        if self._generations.get(skill_name, 0) >= self._config.max_generations:
            return False
        return True

    def validate_candidate(self, candidate: Candidate, seed: Candidate) -> bool:
        """Post-mutation checks: intent preserved, token budget, anchor distance, oscillation."""
        if not self.intent_preserved(candidate.text, seed.text):
            return False
        if candidate.token_count > seed.token_count * self._config.max_token_ratio:
            return False
        dist = self.anchor_distance(candidate.text, seed.text)
        if dist > self._config.anchor_distance_threshold:
            return False
        return True

    def extract_intent(self, text: str) -> str:
        """Extract content under the SKILL INTENT [IMMUTABLE] header."""
        match = _INTENT_RE.search(text)
        if match:
            return match.group(1).strip()
        return ""

    def intent_preserved(self, candidate_text: str, seed_text: str) -> bool:
        """Check that the immutable intent header is unchanged."""
        return self.extract_intent(candidate_text) == self.extract_intent(seed_text)

    def anchor_distance(self, candidate_text: str, seed_text: str) -> float:
        """Compute distance between candidate and seed using SequenceMatcher.

        Returns 0.0 for identical text, 1.0 for completely different text.
        Uses 1 - SequenceMatcher.ratio() as the distance metric.
        """
        ratio = SequenceMatcher(None, candidate_text, seed_text).ratio()
        return 1.0 - ratio

    def is_oscillation(
        self,
        candidate: Candidate,
        recent_versions: list[Candidate],
    ) -> bool:
        """Detect if candidate is cycling back to a recent version.

        Uses SequenceMatcher ratio: if candidate is within 0.05 distance
        of any recent version, it's considered oscillation.
        """
        for recent in recent_versions:
            if candidate.fingerprint == recent.fingerprint:
                return True
            dist = self.anchor_distance(candidate.text, recent.text)
            if dist < self._config.oscillation_distance_threshold:
                return True
        return False

    def set_cooloff(self, skill_name: str, until_turn: int) -> None:
        """Set cooloff period for a skill."""
        self._cooloffs[skill_name] = until_turn

    def in_cooloff(self, skill_name: str, current_turn: int) -> bool:
        """Check if a skill is in cooloff period."""
        until = self._cooloffs.get(skill_name)
        if until is None:
            return False
        return current_turn < until

    def set_generation(self, skill_name: str, generation: int) -> None:
        """Set current generation count for a skill."""
        self._generations[skill_name] = generation

    def get_generation(self, skill_name: str) -> int:
        """Get current generation count for a skill."""
        return self._generations.get(skill_name, 0)

    def _is_exempt(self, skill_tags: list[str]) -> bool:
        """Check if any skill tag matches the exempt list."""
        exempt_set = set(self._config.exempt_tags)
        return bool(exempt_set & set(skill_tags))


def _resolve(base: BoundLimits, override: ChangeBoundConfig | None) -> BoundLimits:
    """Tighten ``base`` by ``override`` (never loosen — federal floor stays non-relaxable)."""
    if override is None:
        return base
    o = override
    return BoundLimits(
        max_edits=min(base.max_edits, o.max_edits) if o.max_edits is not None else base.max_edits,
        edit_schedule=o.edit_schedule or base.edit_schedule,
        min_edits_floor=(
            o.min_edits_floor if o.min_edits_floor is not None else base.min_edits_floor
        ),
        max_files_touched=(
            min(base.max_files_touched, o.max_files_touched)
            if o.max_files_touched is not None
            else base.max_files_touched
        ),
        max_lines_changed=(
            min(base.max_lines_changed, o.max_lines_changed)
            if o.max_lines_changed is not None
            else base.max_lines_changed
        ),
        max_ast_distance=_tighten_distance(base.max_ast_distance, o.max_ast_distance),
        max_prose_edit_distance=(
            min(base.max_prose_edit_distance, o.max_prose_edit_distance)
            if o.max_prose_edit_distance is not None
            else base.max_prose_edit_distance
        ),
    )


def _tighten_distance(base: float, override: float | None) -> float:
    """Tighten a distance ceiling. ``0.0`` means disabled, so any positive override is tighter."""
    if override is None:
        return base
    if base == 0.0:
        return override
    return min(base, override)


def _edit_ops(patch: BundlePatch, base_scripts: dict[str, bytes]) -> int:
    """Count SkillOpt edit operations = non-equal line hunks across all patched files."""
    total = 0
    for rel, new in patch.files.items():
        old_lines = base_scripts.get(rel, b"").decode("utf-8", "replace").splitlines()
        new_lines = new.decode("utf-8", "replace").splitlines()
        matcher = SequenceMatcher(None, old_lines, new_lines)
        total += sum(1 for tag, *_ in matcher.get_opcodes() if tag != "equal")
    return total


def _ast_distance(patch: BundlePatch, base_scripts: dict[str, bytes]) -> float:
    """Regularizer proxy: worst normalized text distance across patched files.

    A char-level ``1 - SequenceMatcher.ratio()`` stand-in for a true AST edit distance
    ([DEEPEN]). Only ever a convergence regularizer — never a security boundary (§8).
    """
    worst = 0.0
    for rel, new in patch.files.items():
        old = base_scripts.get(rel, b"").decode("utf-8", "replace")
        ratio = SequenceMatcher(None, old, new.decode("utf-8", "replace")).ratio()
        worst = max(worst, 1.0 - ratio)
    return worst
