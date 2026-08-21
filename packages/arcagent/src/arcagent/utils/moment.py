"""Cheap cue tokenization for ``agent:moment`` emission (SPEC-071).

The loop announces candidate moments over the module bus so a Brain
(arcmemory) can decide whether to fire a proactive recall. The
``entity_seen``/``topic_shift`` detectors need non-empty ``cues``, but
arcagent does not compute cues today (the Brain derives them internally for
``retrieve``). So each emit site derives cheap cues locally with this shared
helper — a seeding heuristic, not a contract, kept in one place so the two
producer sites (``core.agent_dispatch`` and ``modules.tasks``) cannot drift.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9]+")


def moment_cues(text: str) -> list[str]:
    """Lowercased, de-duplicated word tokens of ``text``, length > 2, in order.

    Order-preserving via ``dict.fromkeys``; a trivial tokenization, not a
    linguistic one — enough to seed the detector's overlap checks.
    """
    return [w for w in dict.fromkeys(_WORD.findall(text.lower())) if len(w) > 2]


__all__ = ["moment_cues"]
