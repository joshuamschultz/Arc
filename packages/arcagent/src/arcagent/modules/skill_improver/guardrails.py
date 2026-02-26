"""Safety guardrails for skill optimization.

Pre-optimization eligibility checks and post-mutation candidate validation.
Prevents catastrophic regression, semantic drift, length explosion,
feedback loops, and unauthorized optimization of security-critical skills.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from arcagent.modules.skill_improver.config import SkillImproverConfig
from arcagent.modules.skill_improver.models import Candidate, SkillTrace

# Regex to extract the immutable intent block
_INTENT_RE = re.compile(
    r"##\s+SKILL INTENT\s+\[IMMUTABLE\]\s*\n(.*?)(?=\n##|\Z)",
    re.DOTALL,
)


class Guardrails:
    """Safety checks for skill optimization eligibility and candidate validation."""

    def __init__(self, config: SkillImproverConfig) -> None:
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
