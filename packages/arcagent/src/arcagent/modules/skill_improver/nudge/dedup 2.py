"""Pre-commit deduplication for nudge candidate skill shapes.

Three checks in order:
1. Name collision: proposed skill name already exists in candidate_store.
2. Fingerprint match: SHA-256 hash of the tool sequence matches an existing
   candidate (exact duplicate workflow).
3. Semantic similarity: cosine similarity of the tool-sequence string against
   known candidate texts exceeds config.trace_similarity_threshold (0.85).

These mirror the dedup strategy described in SDD §3.7 and reuse:
- candidate_store._validate_skill_name (path-safety check)
- Candidate.fingerprint (SHA-256 of text)
- config.trace_similarity_threshold (0.85 default)

Intentionally NO imports from nudge_emitter — this is a pure helper.
"""

from __future__ import annotations

import hashlib
import logging
import re

_logger = logging.getLogger("arcagent.modules.skill_improver.nudge.dedup")

# Pattern mirrors candidate_store._SAFE_NAME_RE exactly
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}$")


def compute_tool_sequence_hash(tool_names: list[str]) -> str:
    """Return SHA-256 of the sorted tool-name sequence.

    Sorted so that different orderings of the same tool set produce the
    same fingerprint — a convention consistent with Candidate.fingerprint
    which hashes body text. We sort here because tool call ORDER varies
    across turns but the SHAPE (which tools were used) is what matters for
    skill dedup.
    """
    joined = ",".join(sorted(tool_names))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _cosine_similarity(vec_a: dict[str, int], vec_b: dict[str, int]) -> float:
    """Compute cosine similarity between two term-frequency vectors.

    Uses term frequencies of individual tool names as the vector space.
    Returns 0.0 if either vector is empty.
    """
    if not vec_a or not vec_b:
        return 0.0

    dot: float = float(sum(vec_a.get(k, 0) * vec_b.get(k, 0) for k in vec_a))
    norm_a: float = float(sum(v * v for v in vec_a.values())) ** 0.5
    norm_b: float = float(sum(v * v for v in vec_b.values())) ** 0.5

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _tool_list_to_tf(tool_names: list[str]) -> dict[str, int]:
    """Convert a list of tool names to a term-frequency dict."""
    tf: dict[str, int] = {}
    for name in tool_names:
        tf[name] = tf.get(name, 0) + 1
    return tf


def is_name_collision(proposed_name: str, existing_names: set[str]) -> bool:
    """Return True if proposed_name already exists in the known skill set.

    Also validates path safety (mirrors candidate_store._validate_skill_name).
    Returns True (collision) for invalid names rather than raising, so that
    the caller can treat it as a dedup hit and suppress the nudge gracefully.
    """
    if not _SAFE_NAME_RE.match(proposed_name):
        _logger.debug(
            "Name '%s' failed path-safety check — treating as collision",
            proposed_name,
        )
        return True
    return proposed_name in existing_names


def is_fingerprint_match(
    tool_sequence_hash: str,
    known_fingerprints: set[str],
) -> bool:
    """Return True if the tool-sequence SHA-256 matches any known candidate."""
    return tool_sequence_hash in known_fingerprints


def is_semantically_similar(
    candidate_tools: list[str],
    known_tool_lists: list[list[str]],
    threshold: float = 0.85,
) -> bool:
    """Return True if the candidate tool list is ≥ threshold similar to any known shape.

    Uses TF-vector cosine similarity as the metric (same approach as
    trace_collector.py's trace_similarity_threshold guidance in config.py).
    """
    candidate_tf = _tool_list_to_tf(candidate_tools)
    for known_tools in known_tool_lists:
        known_tf = _tool_list_to_tf(known_tools)
        sim = _cosine_similarity(candidate_tf, known_tf)
        if sim >= threshold:
            _logger.debug(
                "Semantic similarity %.3f >= threshold %.3f — dedup hit",
                sim,
                threshold,
            )
            return True
    return False


def pre_commit_dedup(
    *,
    proposed_name: str,
    existing_names: set[str],
    tool_sequence_hash: str,
    known_fingerprints: set[str],
    candidate_tools: list[str],
    known_tool_lists: list[list[str]],
    similarity_threshold: float = 0.85,
) -> tuple[bool, str]:
    """Run all three dedup checks in sequence.

    Returns (is_duplicate, reason_str).
    Short-circuits on first match (name > fingerprint > semantic).
    """
    if is_name_collision(proposed_name, existing_names):
        return True, "name_collision"

    if is_fingerprint_match(tool_sequence_hash, known_fingerprints):
        return True, "fingerprint_match"

    if is_semantically_similar(candidate_tools, known_tool_lists, similarity_threshold):
        return True, "semantic_similarity"

    return False, ""
