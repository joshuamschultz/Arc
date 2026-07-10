"""Toggle precedence + audited flips for suite auto-generation (SPEC-054 REQ-113/114, COMP-008).

Layered switches (adapter master, global ``suite.autogen``, per-skill frontmatter) resolve
ONCE per pass into an immutable :class:`ToggleSnapshot` — deny-wins: adapter-off or
global-off dominates a per-skill enable (recorded as ``OVERRIDDEN``), and exempt tags are
never an enable path. Numeric frontmatter overrides merge tighten-only (``min()`` against
the global ceiling), mirroring ChangeBoundConfig. Every effective-autogen flip emits one
``config_change`` audit event before the new snapshot takes effect, at-most-once per
state — the arcllm D-444 pattern (ASI06 defense).
"""

from __future__ import annotations

from typing import Any

from arctrust.audit import AuditEvent, AuditSink, emit
from pydantic import BaseModel, ConfigDict

from arcskill.improver.config import ImproverConfig, SuiteConfig

OVERRIDDEN: str = "OVERRIDDEN"
"""Reason recorded when a deny-level switch forces off a per-skill enable."""

_TIGHTEN_MIN_FIELDS = ("min_cases", "max_cases", "candidate_budget", "flake_runs")
_TIGHTEN_AND_FIELDS = ("generate_on_create", "extend_after_mutation")


class ToggleSnapshot(BaseModel):
    """Immutable per-pass toggle resolution (ASI06 — binds at resolve time)."""

    model_config = ConfigDict(frozen=True)

    suite: SuiteConfig
    """Effective suite settings after deny-wins + tighten-only merge."""

    reasons: dict[str, str]
    """Fields whose requested value was overridden, keyed by dotted field name."""


class ToggleResolver:
    """Resolves layered toggles into snapshots and audits every autogen flip."""

    def __init__(self, *, sink: AuditSink | None, actor_did: str) -> None:
        self._sink = sink
        self._actor_did = actor_did
        # Baseline in-effect state before the first resolve: autogen defaults on.
        self._effective_autogen = True
        self.current: ToggleSnapshot | None = None

    def resolve(
        self,
        *,
        config: ImproverConfig,
        adapter_enabled: bool,
        frontmatter: dict[str, Any] | None = None,
        skill_tags: list[str] | None = None,  # never an enable path (REQ-114)
    ) -> ToggleSnapshot:
        """Resolve the effective suite settings for one mutation unit.

        Values are copied out of ``frontmatter`` at call time, so later mutation
        of the dict cannot reach the returned snapshot (ASI06).
        """
        fm_suite: dict[str, Any] = ((frontmatter or {}).get("improver") or {}).get("suite") or {}
        reasons: dict[str, str] = {}

        requested = bool(fm_suite.get("autogen", True))
        deny = not (adapter_enabled and config.suite.autogen)
        if requested and deny:
            reasons["suite.autogen"] = OVERRIDDEN

        merged = config.suite.model_dump()
        merged["autogen"] = requested and not deny
        for field in _TIGHTEN_MIN_FIELDS:
            if field in fm_suite:
                merged[field] = min(merged[field], int(fm_suite[field]))
        for field in _TIGHTEN_AND_FIELDS:
            if field in fm_suite:
                merged[field] = merged[field] and bool(fm_suite[field])

        snapshot = ToggleSnapshot(suite=SuiteConfig(**merged), reasons=reasons)
        self._maybe_audit_flip(new=snapshot.suite.autogen)
        self.current = snapshot
        return snapshot

    def _maybe_audit_flip(self, *, new: bool) -> None:
        """Emit one ``config_change`` per state change, before the flip takes effect."""
        old = self._effective_autogen
        if new == old:
            return
        if self._sink is not None:
            emit(
                AuditEvent(
                    actor_did=self._actor_did,
                    action="config_change",
                    target="improver.suite",
                    outcome="ok",
                    extra={"field": "suite.autogen", "from": old, "to": new},
                ),
                self._sink,
            )
        self._effective_autogen = new


__all__ = ["OVERRIDDEN", "ToggleResolver", "ToggleSnapshot"]
