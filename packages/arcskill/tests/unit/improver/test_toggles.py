"""SPEC-054 Phase 1 — toggle precedence + audited flips (REQ-113/114, COMP-008).

The toggle layer resolves layered switches (adapter master, global suite.autogen,
per-skill frontmatter, exempt tags) ONCE per pass into an immutable ToggleSnapshot.
Deny-wins precedence: global-off dominates per-skill-on (audited OVERRIDDEN); exempt
tags can never re-enable a globally disabled subsystem. Every flip emits one
``config_change`` audit event (from/to/actor/timestamp) BEFORE the new state takes
effect, at-most-once per flip — the arcllm D-444 pattern (ASI06 defense).
"""

from __future__ import annotations

import dataclasses

import pytest
from arcskill.improver.config import ImproverConfig, SuiteConfig
from arcskill.improver.toggles import OVERRIDDEN, ToggleResolver, ToggleSnapshot
from arctrust.audit import AuditEvent
from pydantic import ValidationError

_ACTOR = "did:arc:test:ops"


class _Sink:
    """AuditSink fake — records events plus the in-effect state at write time."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.autogen_at_write: list[bool | None] = []
        self.resolver: ToggleResolver | None = None

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)
        if self.resolver is not None:
            current = self.resolver.current
            self.autogen_at_write.append(None if current is None else current.suite.autogen)


def _resolver(sink: _Sink | None = None) -> ToggleResolver:
    r = ToggleResolver(sink=sink, actor_did=_ACTOR)
    if sink is not None:
        sink.resolver = r
    return r


def _config_changes(sink: _Sink) -> list[AuditEvent]:
    return [e for e in sink.events if e.action == "config_change"]


class TestTogglePrecedence:
    """REQ-114 — deterministic deny-wins precedence, resolved once per pass."""

    def test_global_autogen_off_dominates_per_skill_on(self) -> None:
        snap = _resolver().resolve(
            config=ImproverConfig(suite=SuiteConfig(autogen=False)),
            adapter_enabled=True,
            frontmatter={"improver": {"suite": {"autogen": True}}},
        )
        assert snap.suite.autogen is False
        assert snap.reasons["suite.autogen"] == OVERRIDDEN

    def test_adapter_master_off_dominates_everything(self) -> None:
        snap = _resolver().resolve(
            config=ImproverConfig(suite=SuiteConfig(autogen=True)),
            adapter_enabled=False,
            frontmatter={"improver": {"suite": {"autogen": True}}},
        )
        assert snap.suite.autogen is False
        assert snap.reasons["suite.autogen"] == OVERRIDDEN

    def test_exempt_tags_never_reenable_under_global_off(self) -> None:
        # exempt_tags exempt a skill from mutation; they must never act as an
        # enable path when the subsystem is globally off.
        snap = _resolver().resolve(
            config=ImproverConfig(suite=SuiteConfig(autogen=False)),
            adapter_enabled=True,
            frontmatter={"improver": {"suite": {"autogen": True}}},
            skill_tags=["security-critical"],
        )
        assert snap.suite.autogen is False

    def test_per_skill_disable_allowed_under_global_on(self) -> None:
        snap = _resolver().resolve(
            config=ImproverConfig(),
            adapter_enabled=True,
            frontmatter={"improver": {"suite": {"autogen": False}}},
        )
        assert snap.suite.autogen is False

    def test_overridden_reason_constant_value(self) -> None:
        assert OVERRIDDEN == "OVERRIDDEN"


class TestFrontmatterTightenOnly:
    """Per-skill overrides merge tighten-only, mirroring ChangeBoundConfig min()."""

    def test_frontmatter_cannot_raise_max_cases_beyond_global(self) -> None:
        snap = _resolver().resolve(
            config=ImproverConfig(suite=SuiteConfig(max_cases=10)),
            adapter_enabled=True,
            frontmatter={"improver": {"suite": {"max_cases": 50}}},
        )
        assert snap.suite.max_cases == 10

    def test_frontmatter_can_lower_max_cases(self) -> None:
        snap = _resolver().resolve(
            config=ImproverConfig(suite=SuiteConfig(max_cases=10)),
            adapter_enabled=True,
            frontmatter={"improver": {"suite": {"max_cases": 4}}},
        )
        assert snap.suite.max_cases == 4


class TestAuditedFlips:
    """REQ-113 — every flip is an audited act, before effect, at-most-once."""

    def test_disable_emits_config_change_with_from_to_actor_timestamp(self) -> None:
        sink = _Sink()
        resolver = _resolver(sink)
        resolver.resolve(
            config=ImproverConfig(suite=SuiteConfig(autogen=False)),
            adapter_enabled=True,
        )
        events = _config_changes(sink)
        assert len(events) == 1
        event = events[0]
        assert event.actor_did == _ACTOR
        assert event.extra["field"] == "suite.autogen"
        assert event.extra["from"] is True
        assert event.extra["to"] is False
        assert event.ts is not None

    def test_flip_event_fires_before_new_state_takes_effect(self) -> None:
        sink = _Sink()
        resolver = _resolver(sink)
        on = resolver.resolve(config=ImproverConfig(), adapter_enabled=True)
        assert on.suite.autogen is True
        resolver.resolve(
            config=ImproverConfig(suite=SuiteConfig(autogen=False)),
            adapter_enabled=True,
        )
        # At sink-write time the in-effect snapshot was still the enabled one.
        assert sink.autogen_at_write == [True]
        current = resolver.current
        assert current is not None
        assert current.suite.autogen is False

    def test_repeated_reads_of_disabled_state_emit_nothing(self) -> None:
        sink = _Sink()
        resolver = _resolver(sink)
        disabled = ImproverConfig(suite=SuiteConfig(autogen=False))
        resolver.resolve(config=disabled, adapter_enabled=True)
        resolver.resolve(config=disabled, adapter_enabled=True)
        resolver.resolve(config=disabled, adapter_enabled=True)
        assert len(_config_changes(sink)) == 1

    def test_each_flip_audited_reenable_emits_again(self) -> None:
        sink = _Sink()
        resolver = _resolver(sink)
        resolver.resolve(
            config=ImproverConfig(suite=SuiteConfig(autogen=False)), adapter_enabled=True
        )
        resolver.resolve(config=ImproverConfig(), adapter_enabled=True)
        events = _config_changes(sink)
        assert len(events) == 2
        assert events[1].extra["from"] is False
        assert events[1].extra["to"] is True


class TestSnapshotImmutability:
    """ASI06 — the per-pass snapshot binds at mutation-unit start and cannot drift."""

    def test_snapshot_is_immutable(self) -> None:
        snap = _resolver().resolve(config=ImproverConfig(), adapter_enabled=True)
        assert isinstance(snap, ToggleSnapshot)
        with pytest.raises((ValidationError, dataclasses.FrozenInstanceError, TypeError)):
            snap.suite = SuiteConfig(autogen=False)  # type: ignore[misc]

    def test_frontmatter_change_mid_unit_does_not_alter_snapshot(self) -> None:
        frontmatter = {"improver": {"suite": {"autogen": False, "max_cases": 4}}}
        snap = _resolver().resolve(
            config=ImproverConfig(),
            adapter_enabled=True,
            frontmatter=frontmatter,
        )
        assert snap.suite.autogen is False
        assert snap.suite.max_cases == 4
        # A mid-unit frontmatter edit must not reach the in-flight snapshot.
        frontmatter["improver"]["suite"]["autogen"] = True
        frontmatter["improver"]["suite"]["max_cases"] = 500
        assert snap.suite.autogen is False
        assert snap.suite.max_cases == 4
