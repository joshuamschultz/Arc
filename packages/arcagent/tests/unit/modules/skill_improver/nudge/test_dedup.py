"""Unit tests for dedup helpers in nudge.dedup."""

from __future__ import annotations

from arcagent.modules.skill_improver.nudge.dedup import (
    compute_tool_sequence_hash,
    is_fingerprint_match,
    is_name_collision,
    is_semantically_similar,
    pre_commit_dedup,
)


class TestComputeToolSequenceHash:
    """compute_tool_sequence_hash returns deterministic SHA-256."""

    def test_order_independent(self) -> None:
        """Same tools in different order produce same hash."""
        h1 = compute_tool_sequence_hash(["read", "bash", "grep"])
        h2 = compute_tool_sequence_hash(["grep", "read", "bash"])
        assert h1 == h2

    def test_empty_list(self) -> None:
        """Empty tool list produces deterministic hash."""
        h = compute_tool_sequence_hash([])
        assert len(h) == 64  # SHA-256 hex

    def test_different_tools_different_hash(self) -> None:
        """Distinct tool sets produce different hashes."""
        h1 = compute_tool_sequence_hash(["read", "bash"])
        h2 = compute_tool_sequence_hash(["read", "write"])
        assert h1 != h2


class TestIsNameCollision:
    """is_name_collision detects existing names and invalid patterns."""

    def test_existing_name_is_collision(self) -> None:
        assert is_name_collision("my-skill", {"my-skill", "other"}) is True

    def test_new_name_no_collision(self) -> None:
        assert is_name_collision("new-skill", {"existing-skill"}) is False

    def test_empty_existing_names(self) -> None:
        assert is_name_collision("valid-name", set()) is False

    def test_invalid_name_treated_as_collision(self) -> None:
        """Names failing path-safety check are treated as collisions."""
        assert is_name_collision("../escape", set()) is True
        assert is_name_collision("", set()) is True
        assert is_name_collision("bad/name", set()) is True


class TestIsFingerprintMatch:
    """is_fingerprint_match checks exact hash equality."""

    def test_match_found(self) -> None:
        h = compute_tool_sequence_hash(["read", "bash"])
        assert is_fingerprint_match(h, {h, "other-hash"}) is True

    def test_no_match(self) -> None:
        h = compute_tool_sequence_hash(["read", "bash"])
        other = compute_tool_sequence_hash(["write"])
        assert is_fingerprint_match(h, {other}) is False

    def test_empty_set_no_match(self) -> None:
        assert is_fingerprint_match("abc123", set()) is False


class TestIsSemanticallySimilar:
    """is_semantically_similar uses TF cosine similarity."""

    def test_identical_tool_lists(self) -> None:
        """Identical tool lists have cosine similarity = 1.0 (> threshold)."""
        tools = ["read", "bash", "grep"]
        assert is_semantically_similar(tools, [tools], threshold=0.85) is True

    def test_completely_different_tools(self) -> None:
        """No overlap = cosine similarity 0."""
        assert (
            is_semantically_similar(
                ["read", "bash"],
                [["write", "delete"]],
                threshold=0.85,
            )
            is False
        )

    def test_empty_candidate(self) -> None:
        assert is_semantically_similar([], [["read"]], threshold=0.85) is False

    def test_empty_known_lists(self) -> None:
        assert is_semantically_similar(["read"], [], threshold=0.85) is False

    def test_above_threshold_threshold(self) -> None:
        """Tools that share most members exceed 0.85 threshold."""
        # ["a","a","b"] vs ["a","b"] — high overlap
        assert (
            is_semantically_similar(
                ["read", "read", "bash"],
                [["read", "bash"]],
                threshold=0.85,
            )
            is True
        )

    def test_below_threshold_not_similar(self) -> None:
        """Very low overlap is below threshold."""
        result = is_semantically_similar(
            ["read"],
            [["write", "delete", "create", "update"]],
            threshold=0.85,
        )
        assert result is False


class TestPreCommitDedup:
    """pre_commit_dedup runs all three checks in sequence."""

    def test_name_collision_short_circuits(self) -> None:
        """Name collision returns True without checking fingerprint or semantic."""
        is_dup, reason = pre_commit_dedup(
            proposed_name="existing",
            existing_names={"existing"},
            tool_sequence_hash="hash-123",
            known_fingerprints=set(),
            candidate_tools=["read"],
            known_tool_lists=[],
        )
        assert is_dup is True
        assert reason == "name_collision"

    def test_fingerprint_match(self) -> None:
        h = compute_tool_sequence_hash(["read", "bash"])
        is_dup, reason = pre_commit_dedup(
            proposed_name="new-skill",
            existing_names=set(),
            tool_sequence_hash=h,
            known_fingerprints={h},
            candidate_tools=["read", "bash"],
            known_tool_lists=[],
        )
        assert is_dup is True
        assert reason == "fingerprint_match"

    def test_semantic_similarity_hit(self) -> None:
        """Identical tool list triggers semantic dedup (when name/fingerprint miss)."""
        is_dup, reason = pre_commit_dedup(
            proposed_name="new-skill",
            existing_names=set(),
            tool_sequence_hash="different-hash",
            known_fingerprints=set(),
            candidate_tools=["read", "bash"],
            known_tool_lists=[["read", "bash"]],  # identical
            similarity_threshold=0.85,
        )
        assert is_dup is True
        assert reason == "semantic_similarity"

    def test_no_dedup_hit(self) -> None:
        """Unique name, hash, and tools pass all dedup checks."""
        is_dup, reason = pre_commit_dedup(
            proposed_name="brand-new-skill",
            existing_names={"other-skill"},
            tool_sequence_hash="unique-hash-xyz",
            known_fingerprints={"other-hash"},
            candidate_tools=["new-tool"],
            known_tool_lists=[["completely", "different", "tools"]],
            similarity_threshold=0.85,
        )
        assert is_dup is False
        assert reason == ""

    def test_dedup_fingerprint_match_test_8(self) -> None:
        """test_dedup_fingerprint_match from test contract (T2.5)."""
        seq_hash = compute_tool_sequence_hash(["read", "bash", "grep"])
        is_dup, reason = pre_commit_dedup(
            proposed_name="new-name",
            existing_names=set(),
            tool_sequence_hash=seq_hash,
            known_fingerprints={seq_hash},
            candidate_tools=["read", "bash", "grep"],
            known_tool_lists=[],
        )
        assert is_dup is True
        assert reason == "fingerprint_match"

    def test_dedup_semantic_similarity_above_threshold_test_9(self) -> None:
        """test_dedup_semantic_similarity_above_threshold from test contract."""
        is_dup, reason = pre_commit_dedup(
            proposed_name="unique-name",
            existing_names=set(),
            tool_sequence_hash="not-in-set",
            known_fingerprints=set(),
            candidate_tools=["read", "bash"],
            known_tool_lists=[["read", "bash"]],  # cosine=1.0 >= 0.85
            similarity_threshold=0.85,
        )
        assert is_dup is True
        assert reason == "semantic_similarity"
