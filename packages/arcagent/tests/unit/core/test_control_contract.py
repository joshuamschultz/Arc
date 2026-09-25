"""Signed control revisions carry complete scope and reject malformed facts."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from arcagent.core.control_contract import SignedControlRevision


def _revision(**updates: object) -> SignedControlRevision:
    fields: dict[str, object] = {
        "tenant_id": "tenant",
        "agent_did": "did:arc:local:agent/one",
        "purpose": "schedule",
        "artifact_id": "schedule-1",
        "revision": 1,
        "definition_digest": "a" * 64,
        "actor_did": "did:arc:local:user/operator",
        "issued_at": datetime.now(UTC),
        "revoked": False,
        "signature": "ab" * 64,
    }
    fields.update(updates)
    return SignedControlRevision.model_validate(fields)


@pytest.mark.parametrize(
    "updates",
    [
        {"revision": 0},
        {"issued_at": datetime(2026, 1, 1)},
        {"definition_digest": "not-a-sha256"},
        {"signature": "not-hex"},
        {"tenant_id": ""},
        {"purpose": "message"},
    ],
)
def test_control_revision_rejects_invalid_scope(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _revision(**updates)


def test_control_revision_is_frozen_and_forbids_extra_fields() -> None:
    revision = _revision()
    with pytest.raises(ValidationError):
        revision.revision = 2
    with pytest.raises(ValidationError):
        _revision(unexpected="untrusted")
