"""Operator approval of pulse checks: status, diff, and the approval writer.

``pulse.md`` is operator-authored and protected from the agent's own tools, but a
check only runs when it carries a revision the control authority signed for its
exact definition. This module is the one production path that issues such a
revision: the operator reviews a check (with a diff against the last approved
definition) and approves it; ``register_revision(purpose="pulse")`` signs it and
this writer stores the returned revision in the check's approval slot. A hand-
edited approval line is worthless -- dispatch re-verifies it against the
authority's head -- so the slot is storage, not trust.

``pulse-approved.json`` keeps the last approved definition per check. It exists
only so the operator can see what changed; it never authorizes anything.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcagent.core.control_contract import (
    ControlActionProofSource,
    ControlArtifactAuthority,
    ControlArtifactRefusedError,
    ControlArtifactUnavailableError,
    SignedControlRevision,
)
from arcagent.modules.pulse import PulseCheck, PulseState
from arcagent.modules.pulse.engine import _SECTION_RE, parse_pulse_file
from arcagent.modules.pulse.signed_dispatch import (
    canonical_definition,
    definition_digest,
    is_approved,
)

PULSE_FILE = "pulse.md"
STATE_FILE = "pulse-state.json"
APPROVED_FILE = "pulse-approved.json"
_APPROVAL_LINE_RE = re.compile(r"^-\s+\*\*Approval:\*\*.*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class PulseCheckStatus:
    """What an operator needs to decide on one pulse check."""

    name: str
    interval_minutes: int
    action: str
    definition_digest: str
    status: str  # approved | unapproved | changes_pending
    approved: bool
    stale: bool
    approved_revision: int | None
    last_revision_ran: int | None
    diff: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "interval_minutes": self.interval_minutes,
            "action": self.action,
            "definition_digest": self.definition_digest,
            "status": self.status,
            "approved": self.approved,
            "stale": self.stale,
            "approved_revision": self.approved_revision,
            "last_revision_ran": self.last_revision_ran,
            "diff": self.diff,
        }


def _definition_lines(definition: dict[str, Any] | None) -> list[str]:
    if definition is None:
        return []
    return [
        f"name: {definition['name']}",
        f"interval_minutes: {definition['interval_minutes']}",
        f"action: {definition['action']}",
    ]


def _definition_of(check: PulseCheck) -> dict[str, Any]:
    return {
        "name": check.name,
        "interval_minutes": check.interval_minutes,
        "action": check.action,
    }


def _diff(previous: dict[str, Any] | None, check: PulseCheck) -> str:
    lines = difflib.unified_diff(
        _definition_lines(previous),
        _definition_lines(_definition_of(check)),
        fromfile="approved",
        tofile="pulse.md",
        lineterm="",
        n=1,
    )
    return "\n".join(lines)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write(path: Path, text: str, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except Exception:  # reason: remove the temp file, then re-raise
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def pulse_status(
    workspace: Path,
    *,
    pulse_file: str = PULSE_FILE,
    state_file: str = STATE_FILE,
    approved_file: str = APPROVED_FILE,
) -> list[PulseCheckStatus]:
    """Review state of every check in ``pulse.md`` (read-only)."""
    try:
        source = (workspace / pulse_file).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    snapshots = _read_json(workspace / approved_file)
    try:
        progress = PulseState(**_read_json(workspace / state_file))
    except ValueError:
        progress = PulseState()
    statuses: list[PulseCheckStatus] = []
    for check in parse_pulse_file(source):
        approval = check.approval
        approved = is_approved(check)
        stale = approval is not None and not approval.revoked and not approved
        snapshot = snapshots.get(check.name)
        previous = snapshot.get("definition") if isinstance(snapshot, dict) else None
        ran = progress.checks.get(check.name)
        statuses.append(
            PulseCheckStatus(
                name=check.name,
                interval_minutes=check.interval_minutes,
                action=check.action,
                definition_digest=definition_digest(check),
                status="approved" if approved else "changes_pending" if stale else "unapproved",
                approved=approved,
                stale=stale,
                approved_revision=None if approval is None else approval.revision,
                last_revision_ran=None if ran is None else ran.last_revision,
                diff="" if approved else _diff(previous, check),
            )
        )
    return statuses


def _with_approval(source: str, name: str, approval: SignedControlRevision) -> str | None:
    """``source`` with ``approval`` stored in check ``name``'s slot; None if absent."""
    sections = list(_SECTION_RE.finditer(source))
    for index, match in enumerate(sections):
        if match.group(1) != name:
            continue
        end = sections[index + 1].start() if index + 1 < len(sections) else len(source)
        body = _APPROVAL_LINE_RE.sub("", source[match.end() : end]).lstrip("\n")
        line = f"- **Approval:** {approval.model_dump_json()}"
        return source[: match.end()] + "\n" + line + "\n" + body + source[end:]
    return None


async def _register(
    check: PulseCheck,
    *,
    expected_revision: int | None,
    tenant_id: str,
    agent_did: str,
    authority: ControlArtifactAuthority,
    actor_proof_source: ControlActionProofSource,
) -> SignedControlRevision:
    definition = canonical_definition(check)
    try:
        proof = await actor_proof_source("pulse", check.name, definition)
        if not isinstance(proof, bytes) or not proof:
            raise ControlArtifactRefusedError("pulse actor proof is absent")
        approval = await authority.register_revision(
            tenant_id=tenant_id,
            agent_did=agent_did,
            purpose="pulse",
            artifact_id=check.name,
            canonical_definition=definition,
            expected_revision=expected_revision,
            actor_proof=proof,
        )
    except ControlArtifactRefusedError:
        raise
    except Exception as exc:
        raise ControlArtifactUnavailableError("pulse approval authority unavailable") from exc
    if (
        approval.tenant_id != tenant_id
        or approval.agent_did != agent_did
        or approval.purpose != "pulse"
        or approval.artifact_id != check.name
        or approval.definition_digest != hashlib.sha256(definition).hexdigest()
        or approval.revoked
    ):
        raise ControlArtifactRefusedError("pulse approval facts differ from the reviewed check")
    return approval


async def approve_pulse_check(
    workspace: Path,
    name: str,
    *,
    reviewed_digest: str,
    tenant_id: str,
    agent_did: str,
    authority: ControlArtifactAuthority,
    actor_proof_source: ControlActionProofSource,
    pulse_file: str = PULSE_FILE,
    approved_file: str = APPROVED_FILE,
) -> SignedControlRevision:
    """Approve check ``name`` exactly as the operator reviewed it.

    ``reviewed_digest`` is the definition digest the operator saw; an edit since
    then is refused so an approval can never cover text the operator did not read.
    """
    path = workspace / pulse_file
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ControlArtifactRefusedError("pulse.md does not exist") from exc
    matches = [check for check in parse_pulse_file(source) if check.name == name]
    if len(matches) != 1:
        raise ControlArtifactRefusedError("pulse check is missing or ambiguous")
    check = matches[0]
    if definition_digest(check) != reviewed_digest:
        raise ControlArtifactRefusedError("pulse check changed since it was reviewed")

    snapshots = _read_json(workspace / approved_file)
    prior = snapshots.get(name)
    if check.approval is not None:
        expected: int | None = check.approval.revision
    else:
        expected = prior.get("revision") if isinstance(prior, dict) else None
    approval = await _register(
        check,
        expected_revision=expected,
        tenant_id=tenant_id,
        agent_did=agent_did,
        authority=authority,
        actor_proof_source=actor_proof_source,
    )

    # Re-read: a concurrent edit keeps its text and the slot goes stale, which is
    # the safe outcome (it needs a fresh approval), so the slot is still written.
    latest = path.read_text(encoding="utf-8")
    updated = _with_approval(latest, name, approval)
    if updated is not None:
        _atomic_write(path, updated, os.stat(path).st_mode & 0o777)
    snapshots[name] = {"revision": approval.revision, "definition": _definition_of(check)}
    _atomic_write(workspace / approved_file, json.dumps(snapshots, indent=2), 0o600)
    return approval


__all__ = [
    "APPROVED_FILE",
    "PULSE_FILE",
    "STATE_FILE",
    "PulseCheckStatus",
    "approve_pulse_check",
    "pulse_status",
]
