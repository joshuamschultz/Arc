"""AuditRedactor — full connector capture, one carve-out (SPEC-062 COMP-014).

REQ-276 is a *capture* requirement, not a redaction one: a federal auditor must be
able to reconstruct **what** happened, not merely that it happened (NIST AU-3). So
every connector call records its arguments and its response in full, labelled with
the tool's declared classification. Stripping PII here would defeat the control the
requirement exists to provide.

Exactly one thing is carved out (REQ-277): a credential read records the store, the
item, the field, the caller, and the outcome — never the value. Without that carve-out
the tamper-evident chain becomes the richest credential database on the box, which is
precisely the "audit store is now the highest-value target" risk the SDD names.

Two design points earn their own note:

* **The carve-out reads the declaration, not the bytes.** Regex detection has false
  negatives by construction, so a credential is dropped because the tool *declared*
  itself a credential read (:data:`CREDENTIAL_READ_TAG`), not because a pattern
  happened to fire. Pattern scanning still runs over ordinary calls as defence in
  depth, using the existing detector in ``arcllm/_pii.py`` — the SECRETS category
  only, since redacting anything else would break reconstruction.
* **The scrub runs here, before emission.** ``arctrust`` is a leaf and must never
  import ``arcllm``; doing so would invert the dependency DAG and end standalone
  packaging of the trust foundation. Emission itself still goes through the single
  ``arctrust.audit.emit`` chokepoint (CON-5).
"""

from __future__ import annotations

import json
from typing import Any

import arctrust
from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.core.tier import Tier
from arcagent.extension.attachment import ToolResult, ToolSpec

#: A tool declaring this capability tag reads a credential, so its arguments and
#: its response are recorded as coordinates only (REQ-277).
CREDENTIAL_READ_TAG = "credential_read"

#: The credential coordinates REQ-277 enumerates. Everything else a declared
#: credential-read tool passes is dropped rather than trusted to be value-free.
_COORDINATE_KEYS = ("store", "item", "field")


class AuditRedactor:
    """Records connector calls in full, with the credential carve-out applied.

    Args:
        audit_sink: Any :class:`~arctrust.audit.AuditSink`; every record reaches it
            through the single :func:`~arctrust.audit.emit` chokepoint.
        tier: Deployment tier stamped on each record.
    """

    def __init__(self, *, audit_sink: AuditSink, tier: Tier) -> None:
        self._sink = audit_sink
        self._tier = tier
        # SECRETS only: the point of REQ-276 is reconstruction, so ordinary PII
        # stays. A leaked credential is the one thing that must not survive.
        self._detector = arctrust.RegexPiiDetector(entities={"allow": [arctrust.SECRETS_CATEGORY]})

    def record_call(
        self,
        *,
        caller_did: str,
        instance: str,
        tool: ToolSpec,
        args: dict[str, Any],
        result: ToolResult,
    ) -> None:
        """Record one connector call and its response (REQ-276).

        A tool declaring :data:`CREDENTIAL_READ_TAG` takes the REQ-277 carve-out
        instead: coordinates and outcome, never the value.
        """
        if CREDENTIAL_READ_TAG in tool.capability_tags:
            store, item, field = (str(args.get(key, "")) for key in _COORDINATE_KEYS)
            self.record_credential_read(
                caller_did=caller_did,
                store=store,
                item=item,
                field=field,
                outcome=result.outcome.value,
            )
            return

        self._emit(
            actor_did=caller_did,
            action="connector.call",
            target=f"connector:{instance}:{tool.name}",
            outcome=result.outcome.value,
            extra={
                "classification": tool.classification,
                "instance": instance,
                "tool": tool.name,
                "arguments": self._scrub(args),
                "result": self._scrub_text(result.content),
            },
        )

    def record_credential_read(
        self,
        *,
        caller_did: str,
        store: str,
        item: str,
        field: str,
        outcome: str,
    ) -> None:
        """Record who read which credential and how it ended — never the value (REQ-277)."""
        self._emit(
            actor_did=caller_did,
            action="credential.read",
            target=f"secret:{store}/{item}#{field}",
            outcome=outcome,
            extra={
                "classification": "read_only",
                "store": store,
                "item": item,
                "field": field,
            },
        )

    def _emit(
        self,
        *,
        actor_did: str,
        action: str,
        target: str,
        outcome: str,
        extra: dict[str, Any],
    ) -> None:
        """Hand one already-scrubbed record to the single emission point (CON-5)."""
        emit(
            AuditEvent(
                actor_did=actor_did,
                action=action,
                target=target,
                outcome=outcome,
                tier=self._tier.value,
                extra=extra,
            ),
            self._sink,
        )

    def _scrub(self, payload: dict[str, Any]) -> Any:
        """Redact detectable secrets from a payload, keeping its structure where possible.

        Serialize, scrub, reparse. A redaction tag can in principle land where it
        breaks the JSON; the scrubbed string is then recorded as-is rather than
        letting the audit path raise on a payload it was asked to preserve.
        """
        raw = json.dumps(payload, default=str, sort_keys=True)
        scrubbed = self._scrub_text(raw)
        if scrubbed == raw:
            return payload
        try:
            return json.loads(scrubbed)
        except json.JSONDecodeError:
            return scrubbed

    def _scrub_text(self, text: str) -> str:
        """Replace any detectable credential with its ``[SECRET:TYPE]`` label."""
        matches = self._detector.detect(text)
        if not matches:
            return text
        return arctrust.redact_text(text, matches)


__all__ = ["CREDENTIAL_READ_TAG", "AuditRedactor"]
