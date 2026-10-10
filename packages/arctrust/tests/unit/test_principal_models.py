"""SPEC-087 T-1272: app principal models, scope catalogue and tier validation.

Covers COMP-001 (``arctrust.principals`` models part):
- REQ-549: a principal's DID is derived from (and bound to) its own Ed25519 key.
- REQ-550: only the ten catalogue scopes are accepted; anything else is refused.
- REQ-551: wildcard agent bindings and wildcard subject allowlists are refused
  at enterprise and federal (allowed at personal).
- REQ-574: expiry is required above personal and bounded (enterprise 90 d,
  federal 30 d); the exact boundary is allowed.

``content_hash`` binds approval rows to the authority fields, so it must be
stable across ordering and blind to lifecycle fields, but must move when any
authority field moves (otherwise a constant hash would pass).

Every refusal carries a stable machine code in ``reason`` that never echoes
attacker-supplied input; the client sees the code, the audit sees the detail.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from nacl.signing import SigningKey

from arctrust.identity import did_from_public_key, did_matches_pubkey
from arctrust.principals import (
    DECISION_SCOPES,
    READ_SCOPES,
    Delegation,
    Principal,
    PrincipalValidationError,
    Scope,
    derive_app_did,
    parse_scope,
    validate_for_tier,
)

ORG = "default"
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
TIERS = ("personal", "enterprise", "federal")
ABOVE_PERSONAL = ("enterprise", "federal")

# The closed set of stable external refusal codes this module may emit.
REASON_CODES = frozenset(
    {
        "unknown_scope",
        "did_mismatch",
        "wildcard_agent",
        "wildcard_subject",
        "missing_expiry",
        "expiry_too_long",
        "unknown_tier",
    }
)
_CODE_SHAPE = re.compile(r"^[a-z][a-z_]*$")

CATALOGUE = {
    "runs:start",
    "runs:read",
    "workflows:run",
    "workflows:event",
    "gates:resolve",
    "tasks:write",
    "inbox:send",
    "memory:write",
    "feedback:write",
    "events:subscribe",
}


# ---------------------------------------------------------------------------
# Fixtures: real Ed25519 keys and real DIDs, never hand-written strings
# ---------------------------------------------------------------------------


def _agent_did() -> str:
    return did_from_public_key(
        bytes(SigningKey.generate().verify_key), org=ORG, agent_type="executor"
    )


def _user_did() -> str:
    return did_from_public_key(bytes(SigningKey.generate().verify_key), org=ORG, agent_type="user")


AGENT_A = _agent_did()
AGENT_B = _agent_did()
PERSON_1 = _user_did()
PERSON_2 = _user_did()
OPERATOR = _user_did()


def _app_key() -> bytes:
    return bytes(SigningKey.generate().verify_key)


def make_principal(**overrides: Any) -> Principal:
    """A principal that is valid at every tier unless a field is overridden."""
    public_key = overrides.pop("public_key_bytes", None) or _app_key()
    fields: dict[str, Any] = {
        "id": "0123456789abcdef0123456789abcdef",
        "name": "billing-bot",
        "did": derive_app_did(public_key, org=ORG),
        "public_key": public_key.hex(),
        "secret_digest": hashlib.sha256(b"not-a-real-secret").hexdigest(),
        "scopes": {
            Scope.RUNS_START: (AGENT_A, AGENT_B),
            Scope.RUNS_READ: (AGENT_A,),
        },
        "subjects": (PERSON_1, PERSON_2),
        "status": "pending",
        "created_by": OPERATOR,
        "approved_by": None,
        "created_at": NOW,
        "expires_at": NOW + timedelta(days=7),
        "last_used_at": None,
        "revoked_by": None,
        "revoked_at": None,
    }
    fields.update(overrides)
    return Principal(**fields)


def _refusal(principal: Principal, tier: str) -> PrincipalValidationError:
    with pytest.raises(PrincipalValidationError) as info:
        validate_for_tier(principal, tier, now=NOW)  # type: ignore[arg-type]  # tier is a str literal from params
    return info.value


# ---------------------------------------------------------------------------
# Scope catalogue (REQ-550)
# ---------------------------------------------------------------------------


class TestScopeCatalogue:
    def test_scope_catalogue_has_exactly_the_ten_values(self) -> None:
        assert {s.value for s in Scope} == CATALOGUE
        assert len(list(Scope)) == 10

    def test_scope_members_compare_equal_to_wire_strings(self) -> None:
        assert Scope.RUNS_START == "runs:start"
        assert Scope.EVENTS_SUBSCRIBE == "events:subscribe"
        assert isinstance(Scope.GATES_RESOLVE, str)

    def test_decision_scopes_are_exactly_gates_and_tasks(self) -> None:
        assert frozenset({Scope.GATES_RESOLVE, Scope.TASKS_WRITE}) == DECISION_SCOPES
        assert isinstance(DECISION_SCOPES, frozenset)

    def test_read_scopes_are_exactly_runs_read_and_events(self) -> None:
        assert frozenset({Scope.RUNS_READ, Scope.EVENTS_SUBSCRIBE}) == READ_SCOPES
        assert isinstance(READ_SCOPES, frozenset)

    def test_decision_and_read_scopes_are_disjoint(self) -> None:
        assert not (DECISION_SCOPES & READ_SCOPES)

    @pytest.mark.parametrize("value", sorted(CATALOGUE))
    def test_parse_scope_known_value_returns_member(self, value: str) -> None:
        parsed = parse_scope(value)
        assert isinstance(parsed, Scope)
        assert parsed.value == value

    @pytest.mark.parametrize(
        "value",
        [
            "admin",
            "runs:*",
            "*",
            "",
            "RUNS:START",
            " runs:start",
            "runs:start ",
            "runs:start\x00",
            "runs_start",
            "approvals:resolve",
        ],
    )
    def test_parse_scope_unknown_value_refused_with_stable_code(self, value: str) -> None:
        with pytest.raises(PrincipalValidationError) as info:
            parse_scope(value)
        assert info.value.reason == "unknown_scope"

    def test_parse_scope_refusal_does_not_echo_input(self) -> None:
        hostile = "inject<script>"
        with pytest.raises(PrincipalValidationError) as info:
            parse_scope(hostile)
        assert hostile not in info.value.reason

    @pytest.mark.parametrize("tier", TIERS)
    def test_validate_raw_unknown_scope_key_refused_at_every_tier(self, tier: str) -> None:
        # A record rehydrated from JSON could carry a raw string key that never
        # passed parse_scope; validation must not trust the type annotation.
        principal = make_principal(scopes={"admin": (AGENT_A,)})
        assert _refusal(principal, tier).reason == "unknown_scope"


# ---------------------------------------------------------------------------
# DID bound to the app's public key (REQ-549)
# ---------------------------------------------------------------------------


class TestDidBinding:
    def test_derive_app_did_matches_identity_derivation_with_app_type(self) -> None:
        key = _app_key()
        did = derive_app_did(key, org=ORG)
        assert did == did_from_public_key(key, org=ORG, agent_type="app")
        assert did.startswith(f"did:arc:{ORG}:app/")
        assert did_matches_pubkey(did, key)

    def test_derive_app_did_differs_per_key(self) -> None:
        assert derive_app_did(_app_key(), org=ORG) != derive_app_did(_app_key(), org=ORG)

    @pytest.mark.parametrize("tier", TIERS)
    def test_validate_matching_did_and_key_accepted(self, tier: str) -> None:
        validate_for_tier(make_principal(), tier, now=NOW)  # type: ignore[arg-type]  # tier is a str literal from params

    @pytest.mark.parametrize("tier", TIERS)
    def test_validate_did_from_another_key_refused(self, tier: str) -> None:
        other_did = derive_app_did(_app_key(), org=ORG)
        principal = make_principal(did=other_did)
        assert _refusal(principal, tier).reason == "did_mismatch"

    @pytest.mark.parametrize("tier", TIERS)
    def test_validate_did_with_right_key_but_non_app_type_refused(self, tier: str) -> None:
        # An app must not claim an agent's DID shape even with its own key hash.
        key = _app_key()
        principal = make_principal(
            public_key_bytes=key,
            did=did_from_public_key(key, org=ORG, agent_type="executor"),
        )
        assert _refusal(principal, tier).reason == "did_mismatch"

    @pytest.mark.parametrize("tier", TIERS)
    def test_validate_malformed_did_refused(self, tier: str) -> None:
        principal = make_principal(did="not-a-did")
        assert _refusal(principal, tier).reason == "did_mismatch"

    @pytest.mark.parametrize("tier", TIERS)
    def test_validate_non_hex_public_key_refused(self, tier: str) -> None:
        principal = make_principal(public_key="zz" * 32)
        assert _refusal(principal, tier).reason in REASON_CODES

    def test_did_mismatch_reason_does_not_echo_did(self) -> None:
        other_did = derive_app_did(_app_key(), org=ORG)
        err = _refusal(make_principal(did=other_did), "personal")
        assert other_did not in err.reason


# ---------------------------------------------------------------------------
# Wildcards (REQ-551)
# ---------------------------------------------------------------------------


class TestWildcards:
    @pytest.mark.parametrize("tier", ABOVE_PERSONAL)
    def test_wildcard_agent_refused_above_personal(self, tier: str) -> None:
        principal = make_principal(scopes={Scope.RUNS_START: ("*",)})
        assert _refusal(principal, tier).reason == "wildcard_agent"

    @pytest.mark.parametrize("tier", ABOVE_PERSONAL)
    def test_wildcard_agent_mixed_with_named_agent_refused_above_personal(self, tier: str) -> None:
        principal = make_principal(
            scopes={Scope.RUNS_READ: (AGENT_A,), Scope.TASKS_WRITE: (AGENT_B, "*")}
        )
        assert _refusal(principal, tier).reason == "wildcard_agent"

    @pytest.mark.parametrize("tier", ABOVE_PERSONAL)
    def test_wildcard_subject_refused_above_personal(self, tier: str) -> None:
        principal = make_principal(subjects=("*",))
        assert _refusal(principal, tier).reason == "wildcard_subject"

    @pytest.mark.parametrize("tier", ABOVE_PERSONAL)
    def test_wildcard_subject_mixed_with_named_person_refused_above_personal(
        self, tier: str
    ) -> None:
        principal = make_principal(subjects=(PERSON_1, "*"))
        assert _refusal(principal, tier).reason == "wildcard_subject"

    def test_wildcard_agent_and_subject_allowed_at_personal(self) -> None:
        principal = make_principal(
            scopes={Scope.RUNS_START: ("*",), Scope.GATES_RESOLVE: ("*",)},
            subjects=("*",),
        )
        validate_for_tier(principal, "personal", now=NOW)

    @pytest.mark.parametrize("tier", ABOVE_PERSONAL)
    def test_named_agents_and_subjects_allowed_above_personal(self, tier: str) -> None:
        # Narrowness: the wildcard rule must not refuse ordinary bindings.
        validate_for_tier(make_principal(), tier, now=NOW)  # type: ignore[arg-type]  # tier is a str literal from params


# ---------------------------------------------------------------------------
# Expiry (REQ-574)
# ---------------------------------------------------------------------------


class TestExpiry:
    @pytest.mark.parametrize("tier", ABOVE_PERSONAL)
    def test_missing_expiry_refused_above_personal(self, tier: str) -> None:
        principal = make_principal(expires_at=None)
        assert _refusal(principal, tier).reason == "missing_expiry"

    def test_missing_expiry_allowed_at_personal(self) -> None:
        validate_for_tier(make_principal(expires_at=None), "personal", now=NOW)

    def test_long_expiry_allowed_at_personal(self) -> None:
        principal = make_principal(expires_at=NOW + timedelta(days=365))
        validate_for_tier(principal, "personal", now=NOW)

    @pytest.mark.parametrize(
        ("tier", "max_days"),
        [("enterprise", 90), ("federal", 30)],
    )
    def test_expiry_at_exact_bound_allowed(self, tier: str, max_days: int) -> None:
        principal = make_principal(expires_at=NOW + timedelta(days=max_days))
        validate_for_tier(principal, tier, now=NOW)  # type: ignore[arg-type]  # tier is a str literal from params

    @pytest.mark.parametrize(
        ("tier", "max_days"),
        [("enterprise", 90), ("federal", 30)],
    )
    def test_expiry_one_second_past_bound_refused(self, tier: str, max_days: int) -> None:
        principal = make_principal(expires_at=NOW + timedelta(days=max_days, seconds=1))
        assert _refusal(principal, tier).reason == "expiry_too_long"

    def test_federal_refuses_expiry_enterprise_would_allow(self) -> None:
        principal = make_principal(expires_at=NOW + timedelta(days=60))
        validate_for_tier(principal, "enterprise", now=NOW)
        assert _refusal(principal, "federal").reason == "expiry_too_long"


# ---------------------------------------------------------------------------
# Tier input and refusal codes
# ---------------------------------------------------------------------------


class TestTierAndReasonCodes:
    @pytest.mark.parametrize("tier", ["", "Federal", "unknown", "personal "])
    def test_unknown_tier_fails_closed(self, tier: str) -> None:
        assert _refusal(make_principal(), tier).reason == "unknown_tier"

    def test_unknown_tier_fails_closed_even_for_personal_only_shapes(self) -> None:
        # A typo must never land on the most permissive tier.
        principal = make_principal(scopes={Scope.RUNS_START: ("*",)}, expires_at=None)
        with pytest.raises(PrincipalValidationError):
            validate_for_tier(principal, "persnal", now=NOW)  # type: ignore[arg-type]  # deliberately invalid tier

    @pytest.mark.parametrize(
        "overrides",
        [
            {"scopes": {Scope.RUNS_START: ("*",)}},
            {"subjects": ("*",)},
            {"expires_at": None},
            {"expires_at": NOW + timedelta(days=400)},
            {"scopes": {"admin": (AGENT_A,)}},
        ],
    )
    def test_reason_is_a_stable_code_from_the_closed_set(self, overrides: dict[str, Any]) -> None:
        err = _refusal(make_principal(**overrides), "federal")
        assert err.reason in REASON_CODES
        assert _CODE_SHAPE.match(err.reason)

    def test_reason_is_identical_across_distinct_offending_inputs(self) -> None:
        first = _refusal(make_principal(did=derive_app_did(_app_key(), org=ORG)), "federal")
        second = _refusal(make_principal(did=derive_app_did(_app_key(), org=ORG)), "federal")
        assert first.reason == second.reason == "did_mismatch"


# ---------------------------------------------------------------------------
# content_hash
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_content_hash_is_sha256_hex(self) -> None:
        digest = make_principal().content_hash()
        assert re.fullmatch(r"[0-9a-f]{64}", digest)

    def test_content_hash_deterministic_for_same_fields(self) -> None:
        key = _app_key()
        first = make_principal(public_key_bytes=key)
        second = make_principal(public_key_bytes=key)
        assert first.content_hash() == second.content_hash()

    def test_content_hash_stable_across_scope_dict_order(self) -> None:
        key = _app_key()
        forward = make_principal(
            public_key_bytes=key,
            scopes={Scope.RUNS_START: (AGENT_A,), Scope.TASKS_WRITE: (AGENT_B,)},
        )
        backward = make_principal(
            public_key_bytes=key,
            scopes={Scope.TASKS_WRITE: (AGENT_B,), Scope.RUNS_START: (AGENT_A,)},
        )
        assert forward.content_hash() == backward.content_hash()

    def test_content_hash_stable_across_agent_order(self) -> None:
        key = _app_key()
        ab = make_principal(public_key_bytes=key, scopes={Scope.RUNS_START: (AGENT_A, AGENT_B)})
        ba = make_principal(public_key_bytes=key, scopes={Scope.RUNS_START: (AGENT_B, AGENT_A)})
        assert ab.content_hash() == ba.content_hash()

    @pytest.mark.parametrize(
        "lifecycle",
        [
            {"status": "active"},
            {"status": "revoked"},
            {"last_used_at": NOW + timedelta(hours=1)},
            {"approved_by": OPERATOR},
            {"revoked_by": OPERATOR},
            {"revoked_at": NOW + timedelta(days=1)},
            {
                "status": "revoked",
                "approved_by": OPERATOR,
                "last_used_at": NOW,
                "revoked_by": OPERATOR,
                "revoked_at": NOW,
            },
        ],
    )
    def test_content_hash_unchanged_by_lifecycle_fields(self, lifecycle: dict[str, Any]) -> None:
        base = make_principal()
        moved = dataclasses.replace(base, **lifecycle)
        assert moved.content_hash() == base.content_hash()

    @pytest.mark.parametrize(
        "authority_change",
        [
            {"name": "other-bot"},
            {"id": "f" * 32},
            {"secret_digest": hashlib.sha256(b"other").hexdigest()},
            {"scopes": {Scope.RUNS_START: (AGENT_A, AGENT_B)}},
            {"scopes": {Scope.RUNS_START: (AGENT_A,), Scope.RUNS_READ: (AGENT_A,)}},
            {"scopes": {Scope.GATES_RESOLVE: (AGENT_A, AGENT_B), Scope.RUNS_READ: (AGENT_A,)}},
            {"subjects": (PERSON_1,)},
            {"subjects": ("*",)},
            {"expires_at": NOW + timedelta(days=8)},
            {"expires_at": None},
            {"created_by": PERSON_1},
        ],
    )
    def test_content_hash_changes_when_authority_field_changes(
        self, authority_change: dict[str, Any]
    ) -> None:
        # Guards against a hash that ignores the fields the approval row binds.
        base = make_principal()
        moved = dataclasses.replace(base, **authority_change)
        assert moved.content_hash() != base.content_hash()

    def test_content_hash_changes_when_key_and_did_change(self) -> None:
        base = make_principal()
        key = _app_key()
        swapped = dataclasses.replace(base, public_key=key.hex(), did=derive_app_did(key, org=ORG))
        assert swapped.content_hash() != base.content_hash()


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestFrozenModels:
    def test_principal_is_frozen(self) -> None:
        principal = make_principal()
        with pytest.raises(dataclasses.FrozenInstanceError):
            principal.status = "active"  # type: ignore[misc]  # asserting the frozen refusal

    def test_delegation_is_frozen_and_carries_fields(self) -> None:
        app_did = derive_app_did(_app_key(), org=ORG)
        delegation = Delegation(
            id="d" * 32,
            person_did=PERSON_1,
            principal_did=app_did,
            scopes=(Scope.GATES_RESOLVE,),
            created_at=NOW,
            expires_at=NOW + timedelta(hours=8),
            revoked=False,
        )
        assert delegation.person_did == PERSON_1
        assert delegation.principal_did == app_did
        assert tuple(delegation.scopes) == (Scope.GATES_RESOLVE,)
        assert delegation.revoked is False
        with pytest.raises(dataclasses.FrozenInstanceError):
            delegation.revoked = True  # type: ignore[misc]  # asserting the frozen refusal


# ---------------------------------------------------------------------------
# Serialization round trip (the store persists plain JSON-safe dicts)
# ---------------------------------------------------------------------------

Mutation = Callable[[dict[str, Any]], object]


def make_delegation(**overrides: Any) -> Delegation:
    fields: dict[str, Any] = {
        "id": "d" * 32,
        "person_did": PERSON_1,
        "principal_did": derive_app_did(_app_key(), org=ORG),
        "scopes": (Scope.GATES_RESOLVE, Scope.TASKS_WRITE),
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=8),
        "revoked": False,
    }
    fields.update(overrides)
    return Delegation(**fields)


def _via_json(data: dict[str, Any]) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(json.dumps(data))
    return loaded


class TestPrincipalSerialization:
    @pytest.mark.parametrize(
        "overrides",
        [
            {},
            {"expires_at": None},
            {
                "status": "revoked",
                "approved_by": OPERATOR,
                "last_used_at": NOW + timedelta(minutes=5),
                "revoked_by": OPERATOR,
                "revoked_at": NOW + timedelta(days=1),
            },
            {"subjects": ("*",), "scopes": {Scope.EVENTS_SUBSCRIBE: ("*",)}},
        ],
    )
    def test_round_trip_through_json_preserves_every_field(
        self, overrides: dict[str, Any]
    ) -> None:
        principal = make_principal(**overrides)
        restored = Principal.from_dict(_via_json(principal.to_dict()))
        assert restored == principal
        assert restored.content_hash() == principal.content_hash()

    def test_from_dict_returns_scope_members_and_tuples(self) -> None:
        restored = Principal.from_dict(_via_json(make_principal().to_dict()))
        assert all(isinstance(scope, Scope) for scope in restored.scopes)
        assert all(isinstance(agents, tuple) for agents in restored.scopes.values())
        assert isinstance(restored.subjects, tuple)

    def test_from_dict_unknown_scope_fails_closed(self) -> None:
        data = make_principal().to_dict()
        data["scopes"] = {"admin": [AGENT_A]}
        with pytest.raises(PrincipalValidationError) as info:
            Principal.from_dict(data)
        assert info.value.reason == "unknown_scope"

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.pop("did"),
            lambda d: d.update(status="superuser"),
            lambda d: d.update(name=7),
            lambda d: d.update(scopes=[["runs:start", [AGENT_A]]]),
            lambda d: d.update(scopes={"runs:start": AGENT_A}),
            lambda d: d.update(subjects="*"),
            lambda d: d.update(subjects=[PERSON_1, 3]),
            lambda d: d.update(created_at="yesterday"),
            lambda d: d.update(created_at="2026-10-10T12:00:00"),
            lambda d: d.update(approved_by=5),
        ],
    )
    def test_from_dict_malformed_record_fails_closed(self, mutate: Mutation) -> None:
        data = make_principal().to_dict()
        mutate(data)
        with pytest.raises(PrincipalValidationError) as info:
            Principal.from_dict(data)
        assert _CODE_SHAPE.match(info.value.reason)

    def test_from_dict_rejects_non_mapping(self) -> None:
        with pytest.raises(PrincipalValidationError):
            Principal.from_dict(["not", "a", "record"])  # type: ignore[arg-type]  # hostile input shape


class TestDelegationSerialization:
    @pytest.mark.parametrize("overrides", [{}, {"revoked": True}])
    def test_round_trip_through_json_preserves_every_field(
        self, overrides: dict[str, Any]
    ) -> None:
        delegation = make_delegation(**overrides)
        assert Delegation.from_dict(_via_json(delegation.to_dict())) == delegation

    def test_from_dict_returns_scope_members(self) -> None:
        restored = Delegation.from_dict(_via_json(make_delegation().to_dict()))
        assert isinstance(restored.scopes, tuple)
        assert all(isinstance(scope, Scope) for scope in restored.scopes)

    def test_from_dict_unknown_scope_fails_closed(self) -> None:
        data = make_delegation().to_dict()
        data["scopes"] = ["gates:resolve", "root"]
        with pytest.raises(PrincipalValidationError) as info:
            Delegation.from_dict(data)
        assert info.value.reason == "unknown_scope"

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.pop("person_did"),
            lambda d: d.update(revoked="no"),
            lambda d: d.update(expires_at=None),
            lambda d: d.update(scopes="gates:resolve"),
        ],
    )
    def test_from_dict_malformed_record_fails_closed(self, mutate: Mutation) -> None:
        data = make_delegation().to_dict()
        mutate(data)
        with pytest.raises(PrincipalValidationError) as info:
            Delegation.from_dict(data)
        assert _CODE_SHAPE.match(info.value.reason)
