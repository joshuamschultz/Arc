"""SPEC-056 Phase C — ``MsgType.TASK_ASSIGNED`` (TDD RED, arcteam side).

Cross-agent task hand-off (SDD §5) needs a dedicated message type distinct
from the existing ``MsgType.TASK`` ("task") — the deepen correction in
PLAN.md's Phase C explicitly calls for a new ``TASK_ASSIGNED = "task_assigned"``
enum member (types.py:27-35) rather than overloading ``TASK`` or introducing a
dotted value (``"task.assigned"`` would collide with nothing today, but the
plan is explicit: no dot). This file only asserts the type exists, participates
in the strict ``MsgType(...)`` validation path, and that a ``Message`` carrying
it signs and verifies exactly like every other ``msg_type`` — i.e. ``msg_type``
is one of the ``_SIGNED_FIELDS`` (crypto.py:24-38), so tampering with it after
signing must invalidate the signature.

RED: ``MsgType.TASK_ASSIGNED`` does not exist yet, so every test here fails
with ``AttributeError`` until the enum member is added.
"""

from __future__ import annotations

from datetime import UTC, datetime

from arctrust import generate_keypair

from arcteam.crypto import new_nonce, sign_message, verify_message
from arcteam.types import Message, MsgType


def _msg(msg_type: MsgType, body: str = "@bob task_id=task_abc123 — ship it") -> Message:
    return Message(
        id="msg_1",
        ts=datetime.now(UTC).isoformat(),
        sender="agent://alice",
        to=["agent://bob"],
        msg_type=msg_type,
        body=body,
    )


class TestTaskAssignedEnumMember:
    def test_member_exists_with_expected_value(self) -> None:
        assert MsgType.TASK_ASSIGNED == "task_assigned"

    def test_distinct_from_existing_task_member(self) -> None:
        # The deepen correction explicitly avoids colliding with the
        # pre-existing MsgType.TASK ("task").
        assert MsgType.TASK_ASSIGNED != MsgType.TASK
        assert MsgType.TASK_ASSIGNED.value != MsgType.TASK.value

    def test_no_dot_in_value(self) -> None:
        # PLAN.md Phase C: "Avoid `task.assigned` (dot)".
        assert "." not in MsgType.TASK_ASSIGNED.value

    def test_strict_construction_accepts_the_value(self) -> None:
        # Strict validation site: MsgType(...) must accept the raw string,
        # exactly like every other member (mirrors test_msg_type_roundtrip).
        assert MsgType("task_assigned") == MsgType.TASK_ASSIGNED

    def test_member_included_in_full_enum_roundtrip(self) -> None:
        assert MsgType.TASK_ASSIGNED in set(MsgType)
        for member in MsgType:
            assert MsgType(member.value) == member


class TestMessageWithTaskAssignedType:
    def test_message_accepts_the_msg_type(self) -> None:
        msg = _msg(MsgType.TASK_ASSIGNED)
        assert msg.msg_type == MsgType.TASK_ASSIGNED

    def test_serialization_roundtrip(self) -> None:
        msg = _msg(MsgType.TASK_ASSIGNED)
        data = msg.model_dump()
        assert data["msg_type"] == "task_assigned"
        restored = Message.model_validate(data)
        assert restored.msg_type == MsgType.TASK_ASSIGNED


class TestSignAndVerifyWithTaskAssignedType:
    """The signature must bind ``msg_type`` — a forged type must fail verify."""

    def test_sign_then_verify_ok(self) -> None:
        kp = generate_keypair()
        msg = _msg(MsgType.TASK_ASSIGNED)
        msg.signer_did = "did:arc:local:agent/alice"
        msg.nonce = new_nonce()
        sign_message(msg, kp.private_key)
        assert msg.sig != ""
        assert verify_message(msg, kp.public_key) is True

    def test_retyping_a_signed_message_fails_verify(self) -> None:
        # Proves msg_type is in _SIGNED_FIELDS: signing as INFO then relabeling
        # to TASK_ASSIGNED (or vice versa) after the fact must invalidate sig —
        # otherwise a tampered envelope could impersonate a task hand-off.
        kp = generate_keypair()
        msg = _msg(MsgType.INFO)
        msg.nonce = new_nonce()
        sign_message(msg, kp.private_key)
        msg.msg_type = MsgType.TASK_ASSIGNED
        assert verify_message(msg, kp.public_key) is False
