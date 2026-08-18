"""Durable plan state — one atomic ``plans/<id>.json`` per plan (SPEC-040).

Reuses the existing atomic-write helper (:func:`arcagent.utils.io.atomic_write_text`)
and the arctrust audit sink — no new store, no new persistence subsystem
(REQ-010). The JSON file is the *operational* resume record; the arctrust
chain is the *compliance* record. Both are written from a single emission
point per transition (:meth:`PlanStore.save`), so the two records never
diverge.

Integrity (ASI06): a malformed/truncated plan file is rejected on read
rather than executed as a corrupt plan (REQ-014).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from arctrust import AuditEvent, AuditSink, emit
from pydantic import ValidationError

from arcagent.modules.planning.models import Plan, PlanStatus
from arcagent.utils.io import atomic_write_text

_logger = logging.getLogger("arcagent.modules.planning.store")


class PlanIntegrityError(ValueError):
    """Raised when a persisted plan is malformed, truncated, or unparseable."""


def _safe_plan_id(plan_id: str) -> str:
    """Reject a plan id that would escape the plans directory.

    Mirrors ``SessionManager._session_jsonl_path``'s traversal guard: a plan
    id is a filename component, never a path.
    """
    if (
        not plan_id
        or "/" in plan_id
        or "\\" in plan_id
        or "\x00" in plan_id
        or plan_id in (".", "..")
    ):
        raise ValueError(f"invalid plan id: {plan_id!r}")
    return plan_id


class PlanStore:
    """Load/save plans durably and audit every transition.

    ``audit_sink`` is the operator-signed arctrust sink where configured; it
    defaults to no sink (the compliance chain is simply not written at a tier
    that has none). ``telemetry`` mirrors transitions to the live observable
    trail. Both are optional so the store works in tests and headless setups.

    ``operator_signer`` closes the F4 gap: the ``plans/<id>.json`` file lives in
    the agent-writable workspace, so an agent with write/bash could flip a step
    FAILED->SUCCEEDED or inject steps and a resume would trust it. When a signer
    is configured, every checkpoint is Ed25519-signed into an operator-owned
    sidecar (beside the workspace, not inside it) and :meth:`load` re-verifies
    it — a tampered or unsigned plan file fails closed (ASI06, SI-7).
    """

    def __init__(
        self,
        plans_dir: Path,
        *,
        audit_sink: AuditSink | None = None,
        operator_signer: Any = None,
        telemetry: Any = None,
        actor_did: str = "",
    ) -> None:
        self._dir = plans_dir
        self._audit_sink = audit_sink
        self._signer = operator_signer
        # Operator-owned integrity store: <agent_root>/.audit/plans, outside the
        # agent's workspace-confined file tools' reach.
        self._sig_dir = plans_dir.parent.parent / ".audit" / "plans"
        self._telemetry = telemetry
        self._actor_did = actor_did or "did:arc:planner"

    def _path(self, plan_id: str) -> Path:
        return self._dir / f"{_safe_plan_id(plan_id)}.json"

    def _sig_path(self, plan_id: str) -> Path:
        return self._sig_dir / f"{_safe_plan_id(plan_id)}.sig"

    def save(
        self,
        plan: Plan,
        *,
        action: str,
        target: str = "",
        outcome: str = "ok",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Atomically checkpoint ``plan`` and audit the transition.

        Called before the orchestrator proceeds to the next step so a crash
        never loses committed progress (REQ-011). ``action`` is the audited
        event name (``plan.created`` / ``plan.step.succeeded`` / …); ``target``
        is the step id for step transitions, else the plan id.
        """
        path = self._path(plan.plan_id)
        plan.touch()
        body = plan.model_dump_json(indent=2)
        atomic_write_text(path, body)
        self._sign(plan.plan_id, body)
        self._emit(
            plan,
            action=action,
            target=target or plan.plan_id,
            outcome=outcome,
            extra=extra,
        )

    def load(self, plan_id: str) -> Plan:
        """Read a plan, rejecting a corrupt OR tampered file (fail-closed)."""
        path = self._path(plan_id)
        if not path.exists():
            raise FileNotFoundError(f"no plan at {path}")
        raw = path.read_text(encoding="utf-8")
        self._verify(plan_id, raw)
        try:
            return Plan.model_validate_json(raw)
        except ValidationError as exc:
            raise PlanIntegrityError(f"plan {plan_id!r} is malformed") from exc

    def _sign(self, plan_id: str, body: str) -> None:
        """Write the operator signature of the plan file to the sidecar."""
        if self._signer is None:
            return
        self._sig_dir.mkdir(parents=True, exist_ok=True)
        signature = self._signer.sign(body.encode("utf-8"))
        atomic_write_text(self._sig_path(plan_id), signature.hex())

    def _verify(self, plan_id: str, raw: str) -> None:
        """Fail closed unless the plan file matches its operator signature."""
        if self._signer is None:
            return
        from arctrust.signer import verify_signature

        sig_path = self._sig_path(plan_id)
        if not sig_path.exists():
            raise PlanIntegrityError(
                f"plan {plan_id!r} has no operator signature — refusing to trust it"
            )
        try:
            signature = bytes.fromhex(sig_path.read_text(encoding="utf-8").strip())
        except ValueError as exc:
            raise PlanIntegrityError(f"plan {plan_id!r} signature is malformed") from exc
        if not verify_signature(
            self._signer.algorithm, raw.encode("utf-8"), signature, self._signer.public_key
        ):
            raise PlanIntegrityError(
                f"plan {plan_id!r} failed operator signature verification — tampered"
            )

    def active_plan(self) -> Plan | None:
        """Return the most-recently-updated ACTIVE plan, or None.

        The resume entry point (REQ-012): the frontier is re-derived from the
        loaded plan's ``depends_on`` + ``SUCCEEDED`` set, not a stored cursor.
        """
        if not self._dir.is_dir():
            return None
        active: list[Plan] = []
        for path in self._dir.glob("*.json"):
            try:
                plan = self.load(path.stem)
            except PlanIntegrityError:
                _logger.warning("skipping corrupt plan file %s", path)
                continue
            if plan.status is PlanStatus.ACTIVE:
                active.append(plan)
        if not active:
            return None
        return max(active, key=lambda p: p.updated_at)

    def _emit(
        self,
        plan: Plan,
        *,
        action: str,
        target: str,
        outcome: str,
        extra: dict[str, Any] | None,
    ) -> None:
        """Single audit emission point — the sink fans out (REQ-013)."""
        payload = {
            "plan_id": plan.plan_id,
            "version": plan.version,
            "status": plan.status.value,
            **(extra or {}),
        }
        if self._audit_sink is not None:
            emit(
                AuditEvent(
                    actor_did=self._actor_did,
                    action=action,
                    target=target,
                    outcome=outcome,
                    extra=payload,
                ),
                self._audit_sink,
            )
        if self._telemetry is not None:
            self._telemetry.audit_event(action, payload)


__all__ = ["PlanIntegrityError", "PlanStore"]
