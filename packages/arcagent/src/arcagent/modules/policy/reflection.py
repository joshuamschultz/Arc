"""Grounded reflection → the EXISTING ACE curator (SPEC-041 Phase 9).

Reflexion (arXiv:2303.11366) says: reflect on a trajectory, store the lesson to
improve later attempts. Arc already has the *store* — the ACE ``PolicyEngine``
(Reflector → deterministic Curator over scored/pruned/capped ``policy.md``
bullets). This module supplies the missing *grounding* and closes the
automated-run gap (REQ-070/072): it turns a consolidation episode into a bounded
:class:`ReflectionGrounding` and feeds it to the **existing** engine. It adds no
second curation algorithm.

Guardrails (REQ-071, ASI01/ASI06):
* Writes go only through the engine, which targets ``policy.md`` / ``policy.pending``
  — **never** ``identity.md`` (the immutable goal file) and **never** an agent
  free-write tool (SPEC-035 protected-path denial still fires on any tool write).
* An empty / ungrounded reflection writes nothing.
* Federal stages the curated bullets to ``policy.pending`` for human approval;
  personal / enterprise auto-apply. Every mutation is audited by the engine.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_GROUNDING_HEADER = (
    "You are reviewing an automated agent run summarized from its memory "
    "consolidation episode (no live chat transcript exists). Evaluate the "
    "agent's observable behavior and outcomes below."
)


class ReflectionGrounding(BaseModel):
    """The bounded, factual basis a session-less reflection is grounded on.

    Assembled from the consolidation episode: what happened
    (``episode_summary``), concrete results (``step_results``), and what went
    wrong (``failures``). If all three are empty the reflection is ungrounded and
    must write nothing.
    """

    episode_summary: str = ""
    step_results: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when there is nothing concrete to reflect on."""
        return not (self.episode_summary.strip() or self.step_results or self.failures)

    def to_messages(self) -> list[dict[str, str]]:
        """Render the grounding as the synthetic transcript the Reflector reads."""
        lines = [_GROUNDING_HEADER, "", f"Episode: {self.episode_summary}".strip()]
        if self.step_results:
            lines.append("Results:")
            lines.extend(f"- {r}" for r in self.step_results)
        if self.failures:
            lines.append("Failures:")
            lines.extend(f"- {f}" for f in self.failures)
        return [{"role": "user", "content": "\n".join(lines)}]


async def reflect_and_curate(
    engine: Any,
    model: Any,
    grounding: ReflectionGrounding,
    *,
    tier: str = "personal",
    session_id: str = "",
) -> bool:
    """Feed a grounded reflection to the existing ACE engine; return whether it ran.

    Session-less (closes REQ-072): grounds on the consolidation episode rather
    than a chat transcript. Federal stages to ``policy.pending`` (REQ-072); other
    tiers auto-apply. No-ops (writes nothing) when the grounding is empty or no
    eval model is available.
    """
    if grounding.is_empty or model is None:
        return False
    await engine.evaluate(
        grounding.to_messages(),
        model,
        session_id=session_id,
        stage=tier == "federal",
    )
    return True


__all__ = ["ReflectionGrounding", "reflect_and_curate"]
