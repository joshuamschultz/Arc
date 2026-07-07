"""SPEC-035 — non-compositional safety (the lethal trifecta), arcagent side.

Two individually-safe tools can compose into a forbidden capability
(``read_private + egress = exfiltration``). The subset-check that DECIDES this
is LIVE in arctrust's ``GlobalLayer`` (see arctrust tests). arcagent's job is
the *deployment mapping*: turn a tool's ``capability_tags`` into the three
trifecta legs and accumulate them per session. This file verifies that arcagent
side — the tag→leg map and the :class:`SessionCapabilityLedger`.
"""

from __future__ import annotations

from arcagent.core.session_internal.capability_ledger import (
    EXTERNAL_COMMS,
    LETHAL_TRIFECTA,
    PRIVATE_DATA,
    UNTRUSTED_INPUT,
    SessionCapabilityLedger,
    legs_for_tags,
)


class TestTagToLegMapping:
    def test_file_read_maps_to_private_data(self) -> None:
        assert legs_for_tags(["file_read"]) == frozenset({PRIVATE_DATA})

    def test_network_egress_maps_to_external_comms(self) -> None:
        assert legs_for_tags(["network_egress"]) == frozenset({EXTERNAL_COMMS})

    def test_web_read_maps_to_both_egress_and_untrusted(self) -> None:
        # OQ-1 proxy: a web/browser read both egresses and ingests untrusted input.
        assert legs_for_tags(["web"]) == frozenset({EXTERNAL_COMMS, UNTRUSTED_INPUT})

    def test_subprocess_maps_to_untrusted_input(self) -> None:
        # A shell/subprocess ingests untrusted content (command output, fetched
        # files, network responses via curl), so bash contributes the
        # untrusted-input leg. It is NOT tagged external_comms — at ent/fed bash
        # runs --network=none, so tagging egress would spuriously trip the gate.
        assert legs_for_tags(["subprocess"]) == frozenset({UNTRUSTED_INPUT})

    def test_bash_tags_yield_only_untrusted_input(self) -> None:
        # bash's full tag set contributes exactly the untrusted-input leg.
        assert legs_for_tags(["subprocess", "file_write", "state_mutation"]) == frozenset(
            {UNTRUSTED_INPUT}
        )

    def test_unknown_tag_maps_to_nothing(self) -> None:
        assert legs_for_tags(["file_write", "state_mutation"]) == frozenset()


class TestSessionAccumulation:
    def test_read_then_egress_completes_trifecta_across_calls(self) -> None:
        ledger = SessionCapabilityLedger()
        sid = "s1"
        # Turn 1: read a private file + ingest untrusted web content.
        ledger.record(sid, legs_for_tags(["file_read"]))
        ledger.record(sid, legs_for_tags(["extract"]))
        # Not yet forbidden — egress leg missing.
        assert not LETHAL_TRIFECTA.issubset(ledger.snapshot(sid))
        # Turn 3: attempt egress. Now the union completes the trifecta.
        after = ledger.snapshot(sid) | legs_for_tags(["network_egress"])
        assert LETHAL_TRIFECTA.issubset(after)

    def test_sessions_are_isolated(self) -> None:
        ledger = SessionCapabilityLedger()
        ledger.record("a", frozenset({PRIVATE_DATA}))
        assert ledger.snapshot("b") == frozenset()

    def test_reset_clears_session(self) -> None:
        ledger = SessionCapabilityLedger()
        ledger.record("a", frozenset({PRIVATE_DATA}))
        ledger.reset("a")
        assert ledger.snapshot("a") == frozenset()
