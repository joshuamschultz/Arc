"""Unit tests for arcui.identity — the canonical DID parser + roster join.

H-007: agent identity must render consistently everywhere — a raw DID, a
short id, and a friendly name should never disagree because two places
parsed the DID differently. These tests pin down the one parser's contract:
valid DIDs, role DIDs with no hash, malformed input, and the roster join
producing "no name" (not a crash) for an unknown DID.
"""

from __future__ import annotations

from arcui.identity import UNKNOWN, parse_did, resolve_agent_identity


class TestParseDidValid:
    def test_executor_did_with_hash(self):
        identity = parse_did("did:arc:local:executor/e347da22")
        assert identity.did == "did:arc:local:executor/e347da22"
        assert identity.platform == "arc"
        assert identity.host == "local"
        assert identity.type == "executor"
        assert identity.short_id == "e347da22"
        assert identity.name is None

    def test_operator_did_with_hash(self):
        identity = parse_did("did:arc:local:operator/bf6ee9f7")
        assert identity.platform == "arc"
        assert identity.host == "local"
        assert identity.type == "operator"
        assert identity.short_id == "bf6ee9f7"

    def test_arbitrary_org_segment_is_the_host(self):
        # arctrust's DID format calls this segment "org"; the dashboard's
        # display contract calls it "host" — same position, same value.
        identity = parse_did("did:arc:doe-lab-3:planner/12ab34cd")
        assert identity.host == "doe-lab-3"
        assert identity.type == "planner"
        assert identity.short_id == "12ab34cd"


class TestParseDidRoleIdentities:
    def test_viewer_role_has_no_hash(self):
        # A role identity (operator/viewer browser sessions) has no keypair
        # behind it, so there is no hash segment — that's a valid shape, not
        # a malformed one, so short_id is "", not UNKNOWN.
        identity = parse_did("did:arc:ui:viewer")
        assert identity.platform == "arc"
        assert identity.host == "ui"
        assert identity.type == "viewer"
        assert identity.short_id == ""

    def test_operator_role_has_no_hash(self):
        identity = parse_did("did:arc:ui:operator")
        assert identity.host == "ui"
        assert identity.type == "operator"
        assert identity.short_id == ""


class TestParseDidMalformed:
    def test_empty_string_is_safe_fallback(self):
        identity = parse_did("")
        assert identity.did == ""
        assert identity.host == UNKNOWN
        assert identity.platform == UNKNOWN
        assert identity.type == UNKNOWN
        assert identity.short_id == UNKNOWN

    def test_non_did_string_is_safe_fallback(self):
        identity = parse_did("not-a-did-at-all")
        assert identity.host == UNKNOWN
        assert identity.platform == UNKNOWN
        assert identity.type == UNKNOWN
        assert identity.short_id == UNKNOWN

    def test_too_few_segments_is_safe_fallback(self):
        identity = parse_did("did:arc")
        assert identity.host == UNKNOWN
        assert identity.platform == UNKNOWN
        assert identity.type == UNKNOWN
        assert identity.short_id == UNKNOWN

    def test_malformed_did_never_raises(self):
        # The parser is a display helper — it must degrade, never 500 a
        # dashboard route because one agent has a corrupt identity string.
        for bad in ("did:", "did::::", "arc:local:executor/e347da22", None):
            parse_did(bad if bad is not None else "")  # no exception


class TestResolveAgentIdentityRosterJoin:
    def test_known_roster_row_attaches_friendly_name(self):
        identity = resolve_agent_identity("did:arc:local:executor/e347da22", "Coder")
        assert identity.name == "Coder"
        assert identity.type == "executor"
        assert identity.short_id == "e347da22"

    def test_unknown_roster_did_still_shows_parsed_parts(self):
        # No roster row matched this DID (name=None) — the identity must
        # still render its parsed parts rather than disappearing.
        identity = resolve_agent_identity("did:arc:local:executor/e347da22", None)
        assert identity.name is None
        assert identity.host == "local"
        assert identity.platform == "arc"
        assert identity.type == "executor"
        assert identity.short_id == "e347da22"

    def test_empty_name_normalizes_to_none(self):
        identity = resolve_agent_identity("did:arc:local:executor/e347da22", "")
        assert identity.name is None
