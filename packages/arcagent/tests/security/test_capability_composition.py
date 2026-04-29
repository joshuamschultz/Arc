"""SPEC-017 R-012 / SDD §5.2 — non-compositional safety.

Two individually-safe tools can compose into a forbidden capability
(e.g. ``read_file + http_request = data exfiltration``). Capability
composition is checked at batch dispatch time: if the union of the
batch's ``capability_tags`` intersects any ``forbidden_compositions``
set, the dispatcher rejects the batch.

This file verifies the :class:`ForbiddenCompositionChecker` helper.
Integration into the tool registry's batch path will land with the
final end-to-end wiring.
"""

from __future__ import annotations

import pytest


class TestEmptyForbiddenList:
    def test_empty_never_rejects(self) -> None:
        from arcagent.core.tool_policy import ForbiddenCompositionChecker

        checker = ForbiddenCompositionChecker(forbidden=[])
        assert checker.is_forbidden({"file_read", "network_egress"}) is False


class TestBasicComposition:
    def test_exact_match_rejected(self) -> None:
        from arcagent.core.tool_policy import ForbiddenCompositionChecker

        checker = ForbiddenCompositionChecker(
            forbidden=[frozenset({"file_read", "network_egress"})]
        )
        assert checker.is_forbidden({"file_read", "network_egress"}) is True

    def test_superset_also_rejected(self) -> None:
        """If a batch contains {a, b, c} and {a, b} is forbidden, reject."""
        from arcagent.core.tool_policy import ForbiddenCompositionChecker

        checker = ForbiddenCompositionChecker(
            forbidden=[frozenset({"file_read", "network_egress"})]
        )
        caps = {"file_read", "network_egress", "file_write"}
        assert checker.is_forbidden(caps) is True

    def test_partial_subset_allowed(self) -> None:
        from arcagent.core.tool_policy import ForbiddenCompositionChecker

        checker = ForbiddenCompositionChecker(
            forbidden=[frozenset({"file_read", "network_egress"})]
        )
        assert checker.is_forbidden({"file_read"}) is False
        assert checker.is_forbidden({"network_egress"}) is False


class TestMultipleForbiddenSets:
    def test_any_matching_set_rejects(self) -> None:
        from arcagent.core.tool_policy import ForbiddenCompositionChecker

        checker = ForbiddenCompositionChecker(
            forbidden=[
                frozenset({"file_read", "network_egress"}),
                frozenset({"bash", "file_write"}),
            ]
        )
        assert checker.is_forbidden({"bash", "file_write"}) is True
        assert checker.is_forbidden({"file_read", "file_write"}) is False


class TestReasonReporting:
    def test_reason_identifies_the_forbidden_set(self) -> None:
        from arcagent.core.tool_policy import ForbiddenCompositionChecker

        forbidden_a = frozenset({"file_read", "network_egress"})
        checker = ForbiddenCompositionChecker(forbidden=[forbidden_a])
        reason = checker.first_forbidden({"file_read", "network_egress"})
        assert reason == forbidden_a

        # Not forbidden → None
        assert checker.first_forbidden({"file_read"}) is None


# --- sanity check: reference docs ----------------------------------------


def test_module_is_importable() -> None:
    pytest.importorskip("arcagent.core.tool_policy")
