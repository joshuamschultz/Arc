"""SPEC-088 T-1331 (RED): ``Task.due_at`` and the tier-gated classification validator.

COMP-002 (SDD): ``Task.due_at: str | None`` is a timezone-aware ISO 8601
timestamp; a naive or unparsable value is refused at construction. The
``classification`` validator reads the validation context keys ``tier`` and
``clearance``:

* ``tier == "federal"`` parses strictly (``arctrust.classification``): an
  unknown label is refused, fail closed (REQ-603).
* any other tier parses leniently: an unknown label becomes ``UNCLASSIFIED``.
* a label above ``clearance`` is refused at every tier (no write-up).
* no tier/clearance in the context keeps today's behavior, so agent-internal
  writers and ``TaskStore._READ_CONTEXT`` reads are unchanged.

``arcstore.tasks`` already exists, so each test imports it and touches the new
behavior inside the test body: a missing feature shows as an assertion or
"DID NOT RAISE" failure per test, never one collection error.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from arctrust.classification import Classification
from pydantic import ValidationError

from arcstore.backends.memory import FakeBackend

_CREATOR = "did:arc:test:intake/creator0"
_AGENT = "did:arc:test:intake/aaaaaaaa"
_TIERS = ("personal", "enterprise", "federal")


def _task(context: dict[str, Any] | None = None, **fields: Any) -> Any:
    from arcstore.tasks import Task

    payload: dict[str, Any] = {"id": "task-intake-1", "title": "Ship it", "creator_did": _CREATOR}
    payload.update(fields)
    return Task.model_validate(payload, context=context)


class TestTaskDueAt:
    """REQ-602: ``due_at`` is optional and always timezone-aware."""

    def test_task_due_at_defaults_to_none(self) -> None:
        task = _task()
        assert "due_at" in type(task).model_fields
        assert task.due_at is None

    def test_task_due_at_tz_aware_utc_is_kept(self) -> None:
        task = _task(due_at="2026-10-12T17:00:00+00:00")
        assert task.due_at == "2026-10-12T17:00:00+00:00"

    def test_task_due_at_tz_aware_offset_keeps_the_same_instant(self) -> None:
        task = _task(due_at="2026-10-12T12:00:00-05:00")
        assert task.due_at is not None
        parsed = datetime.fromisoformat(task.due_at)
        assert parsed.tzinfo is not None
        assert parsed == datetime(2026, 10, 12, 17, 0, tzinfo=UTC)

    def test_task_due_at_naive_timestamp_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="due_at"):
            _task(due_at="2026-10-12T17:00:00")

    def test_task_due_at_date_only_is_refused(self) -> None:
        # A bare date has no timezone; it is naive and must not be guessed.
        with pytest.raises(ValidationError, match="due_at"):
            _task(due_at="2026-10-12")

    @pytest.mark.parametrize("bad", ["tomorrow", "", "2026-13-40T00:00:00+00:00"])
    def test_task_due_at_unparsable_is_refused(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="due_at"):
            _task(due_at=bad)

    async def test_task_due_at_round_trips_through_task_store(self) -> None:
        from arcstore.tasks import TaskStore

        backend = FakeBackend()
        await backend.start()
        store = TaskStore(backend)
        created = await store.create(_task(owner_did=_AGENT, due_at="2026-10-12T17:00:00+00:00"))

        loaded = await store.get(created.id)

        assert loaded is not None
        assert loaded.due_at == "2026-10-12T17:00:00+00:00"


class TestClassificationTierGate:
    """REQ-603: the classification label is parsed against the caller's tier."""

    def test_task_bogus_classification_refused_at_federal(self) -> None:
        with pytest.raises(ValidationError, match="classification"):
            _task({"tier": "federal", "clearance": "TOP_SECRET"}, classification="BOGUS")

    def test_task_empty_classification_refused_at_federal(self) -> None:
        with pytest.raises(ValidationError, match="classification"):
            _task({"tier": "federal", "clearance": "TOP_SECRET"}, classification="")

    @pytest.mark.parametrize("tier", ["personal", "enterprise"])
    def test_task_bogus_classification_normalized_to_unclassified_below_federal(
        self, tier: str
    ) -> None:
        task = _task({"tier": tier, "clearance": "TOP_SECRET"}, classification="BOGUS")
        assert task.classification == Classification.UNCLASSIFIED.name

    def test_task_known_label_normalized_to_canonical_name_at_federal(self) -> None:
        task = _task({"tier": "federal", "clearance": "SECRET"}, classification=" cui ")
        assert task.classification == Classification.CUI.name

    @pytest.mark.parametrize("tier", _TIERS)
    def test_task_secret_refused_when_clearance_is_cui(self, tier: str) -> None:
        with pytest.raises(ValidationError, match="classification"):
            _task({"tier": tier, "clearance": "CUI"}, classification="SECRET")

    @pytest.mark.parametrize("tier", _TIERS)
    def test_task_label_equal_to_clearance_is_accepted(self, tier: str) -> None:
        task = _task({"tier": tier, "clearance": "CUI"}, classification="CUI")
        assert task.classification == Classification.CUI.name

    @pytest.mark.parametrize("tier", _TIERS)
    def test_task_label_below_clearance_is_accepted(self, tier: str) -> None:
        task = _task({"tier": tier, "clearance": "SECRET"}, classification="UNCLASSIFIED")
        assert task.classification == Classification.UNCLASSIFIED.name


class TestClassificationWithoutContextIsUnchanged:
    """Agent-internal writers and stored-row reads pass no tier: keep today's behavior."""

    def test_task_no_context_keeps_free_string_classification(self) -> None:
        task = _task(classification="BOGUS")
        assert task.classification == "BOGUS"

    def test_task_no_context_keeps_label_above_any_clearance(self) -> None:
        task = _task(classification="TOP_SECRET")
        assert task.classification == "TOP_SECRET"

    def test_task_read_context_without_tier_keeps_current_behavior(self) -> None:
        from arcstore.tasks import TaskStore

        # TaskStore loads every persisted row with _READ_CONTEXT; a stored row
        # must read back exactly, whatever tier wrote it.
        task = _task(dict(TaskStore._READ_CONTEXT), classification="BOGUS")
        assert task.classification == "BOGUS"

    def test_task_default_classification_is_unclassified(self) -> None:
        assert _task().classification == "UNCLASSIFIED"
