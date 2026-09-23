"""ChangeSafetyGate — pure dry-run cross-field validation (SPEC-085 COMP-006, REQ-464).

``SignedSettingsWriter`` (COMP-004) validates a single field against its own
schema before writing. That check cannot see cross-field breakage — e.g. a
``security.custody`` value that is fine in isolation but violates the
federal tier crypto floor once combined with ``security.tier == "federal"``
(``SecurityConfig._enforce_tier_crypto_floor``, SC-5/SC-13/IA-7).

:func:`check_change_safe` re-validates the *whole* ``ArcAgentConfig`` with the
proposed change applied by re-running Pydantic's ``model_validate`` — which
re-triggers every ``model_validator(mode="after")`` hook — and reports
whether the resulting config would still be valid. It never mutates the
config it is handed and never touches the filesystem.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from arcagent.core.config import ArcAgentConfig


class SafetyResult(BaseModel):
    """Outcome of a dry-run change-safety check."""

    safe: bool
    reason: str = ""


def _deep_set(data: dict[str, Any], dotted_field: str, value: Any) -> None:
    """Set ``value`` at ``dotted_field`` inside ``data``, descending into nested dicts."""
    parts = dotted_field.split(".")
    cursor = data
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def check_change_safe(*, field: str, value: Any, config: ArcAgentConfig) -> SafetyResult:
    """Dry-run whether applying ``value`` at dotted ``field`` keeps ``config`` valid.

    Re-validates the whole config (not just the changed field) so cross-field
    ``model_validator`` hooks — invisible to a per-field schema check — get a
    chance to reject the change. Pure: does not mutate ``config`` and performs
    no I/O.
    """
    data = config.model_dump(mode="python")
    _deep_set(data, field, value)

    try:
        ArcAgentConfig.model_validate(data)
    except ValidationError as exc:
        return SafetyResult(safe=False, reason=str(exc))

    return SafetyResult(safe=True, reason="")
