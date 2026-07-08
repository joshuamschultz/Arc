"""Candidate store — persist optimization candidates and audit trail.

Manages the per-skill directory structure for seed snapshots,
candidate versions, manifest (frontier state + lineage), and
append-only audit log for NIST AU-3 compliance.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, cast

from arctrust import AuditEvent, AuditSink, emit

from arcskill.improver._util import atomic_write_text
from arcskill.improver.models import Candidate, MutationEvent

_logger = logging.getLogger("arcskill.improver.candidate_store")

# Strict patterns for path-safe identifiers (ASI-02 defense)
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}$")
_SAFE_CANDIDATE_ID_RE = re.compile(r"^[a-f0-9-]{1,40}$|^seed$")


def _mutation_audit_event(skill_name: str, event: MutationEvent) -> AuditEvent:
    """Map a :class:`MutationEvent` onto the arctrust audit schema."""
    return AuditEvent(
        actor_did="did:arc:skill-improver",
        action="skill.mutation.applied",
        target=skill_name,
        outcome=event.stop_reason,
        payload_hash=event.new_hash,
        extra=event.to_dict(),
    )


def _validate_skill_name(name: str) -> None:
    """Reject skill names that could escape the workspace via path traversal."""
    if not _SAFE_NAME_RE.match(name):
        msg = f"Invalid skill name: {name!r}"
        raise ValueError(msg)


def _validate_candidate_id(cid: str) -> None:
    """Reject candidate IDs that could escape the candidates directory."""
    if not _SAFE_CANDIDATE_ID_RE.match(cid):
        msg = f"Invalid candidate ID: {cid!r}"
        raise ValueError(msg)


class CandidateStore:
    """Persist skill optimization candidates and audit trail."""

    def __init__(self, workspace: Path, *, audit_sink: AuditSink | None = None) -> None:
        self._workspace = workspace
        # SPEC-033 D4/REQ-022 — mutation audit routes through the tamper-evident
        # WORM chain (a ``WormSink`` in production), stored separately from the
        # mutable skill code (AU-9(2)). No plaintext JSONL: it can be silently
        # truncated/reordered, which defeats the audit's purpose.
        self._audit_sink = audit_sink

    def _skill_dir(self, skill_name: str) -> Path:
        """Base directory for a skill's trace and candidate data."""
        _validate_skill_name(skill_name)
        result = (self._workspace / "skill_traces" / skill_name).resolve()
        base = (self._workspace / "skill_traces").resolve()
        if not result.is_relative_to(base):
            msg = f"Skill name escapes workspace: {skill_name!r}"
            raise ValueError(msg)
        return result

    def _candidates_dir(self, skill_name: str) -> Path:
        d = self._skill_dir(skill_name) / "candidates"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _manifest_path(self, skill_name: str) -> Path:
        return self._candidates_dir(skill_name) / "manifest.json"

    def save(
        self,
        skill_name: str,
        candidate: Candidate,
        *,
        active: bool = False,
        frontier: bool = False,
    ) -> None:
        """Save a candidate and update manifest."""
        _validate_candidate_id(candidate.id)
        candidates_dir = self._candidates_dir(skill_name)
        # Write candidate text
        candidate_path = candidates_dir / f"{candidate.id}.md"
        candidate_path.write_text(candidate.text, encoding="utf-8")

        # Update manifest
        manifest = self.load_manifest(skill_name)
        manifest.setdefault("candidates", {})[candidate.id] = {
            "generation": candidate.generation,
            "parent_id": candidate.parent_id,
            "scores": candidate.aggregate_scores,
        }
        if active:
            manifest["active_candidate_id"] = candidate.id
            manifest["generation"] = candidate.generation
        if frontier:
            frontier_ids = manifest.setdefault("frontier", [])
            if candidate.id not in frontier_ids:
                frontier_ids.append(candidate.id)

        self._save_manifest(skill_name, manifest)

    def load(self, skill_name: str, candidate_id: str) -> Candidate | None:
        """Load a candidate by ID."""
        _validate_candidate_id(candidate_id)
        candidate_path = self._candidates_dir(skill_name) / f"{candidate_id}.md"
        if not candidate_path.exists():
            return None
        text = candidate_path.read_text(encoding="utf-8")
        manifest = self.load_manifest(skill_name)
        meta = manifest.get("candidates", {}).get(candidate_id, {})
        return Candidate(
            id=candidate_id,
            text=text,
            aggregate_scores=meta.get("scores", {}),
            token_count=len(text.split()),
            parent_id=meta.get("parent_id"),
            generation=meta.get("generation", 0),
        )

    def get_active(self, skill_name: str) -> Candidate | None:
        """Get the currently active candidate."""
        manifest = self.load_manifest(skill_name)
        active_id = manifest.get("active_candidate_id")
        if not active_id:
            return None
        return self.load(skill_name, active_id)

    def save_seed(self, skill_name: str, text: str) -> None:
        """Save seed snapshot (only on first call, never overwrite)."""
        seed_path = self._candidates_dir(skill_name) / "seed.md"
        if seed_path.exists():
            return  # Never overwrite seed
        seed_path.write_text(text, encoding="utf-8")

    def load_seed(self, skill_name: str) -> str | None:
        """Load the original seed text."""
        seed_path = self._candidates_dir(skill_name) / "seed.md"
        if not seed_path.exists():
            return None
        return seed_path.read_text(encoding="utf-8")

    def append_audit(self, skill_name: str, event: MutationEvent) -> None:
        """Emit a mutation event to the tamper-evident WORM chain (AU-9/AU-10).

        Routes through the injected arctrust ``AuditSink`` — a ``WormSink`` in
        production: Ed25519-signed, hash-linked, append-only, stored apart from
        the skill code. A no-op when no sink is configured (the caller decides
        whether a run is audited); there is no plaintext fallback.
        """
        _validate_skill_name(skill_name)
        if self._audit_sink is None:
            return
        emit(_mutation_audit_event(skill_name, event), self._audit_sink)

    def list_skills(self) -> list[str]:
        """Enumerate skills that have a trace/candidate directory under the workspace."""
        base = (self._workspace / "skill_traces").resolve()
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir())

    def lifecycle_state(self, skill_name: str) -> str:
        """Current lifecycle state (``active`` when unset)."""
        return str(self.load_manifest(skill_name).get("lifecycle_state", "active"))

    def set_lifecycle_state(self, skill_name: str, state: str, *, reason: str = "") -> str:
        """Persist a lifecycle-state transition; return the previous state.

        Retire is non-destructive (D-8): the candidates/lineage are retained and the
        prior active id is recorded so :meth:`revive` can restore it.
        """
        _validate_skill_name(skill_name)
        manifest = self.load_manifest(skill_name)
        previous = str(manifest.get("lifecycle_state", "active"))
        manifest["lifecycle_state"] = state
        if state == "retired":
            manifest["retire_reason"] = reason
            manifest.setdefault("prior_active_id", manifest.get("active_candidate_id"))
        self._save_manifest(skill_name, manifest)
        return previous

    def rollback(self, skill_name: str, candidate_id: str) -> None:
        """Revert to a previous candidate version."""
        _validate_candidate_id(candidate_id)
        candidate = self.load(skill_name, candidate_id)
        if candidate is None:
            msg = f"Candidate '{candidate_id}' not found for skill '{skill_name}'"
            raise ValueError(msg)

        manifest = self.load_manifest(skill_name)
        manifest["active_candidate_id"] = candidate_id
        self._save_manifest(skill_name, manifest)

    def load_manifest(self, skill_name: str) -> dict[str, Any]:
        """Load the manifest file, returning empty dict if not found."""
        manifest_path = self._manifest_path(skill_name)
        if not manifest_path.exists():
            return {"skill_name": skill_name, "frontier": [], "candidates": {}}
        try:
            return cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return {"skill_name": skill_name, "frontier": [], "candidates": {}}

    def _save_manifest(self, skill_name: str, manifest: dict[str, Any]) -> None:
        """Write manifest atomically via tmp + rename."""
        manifest_path = self._manifest_path(skill_name)
        atomic_write_text(manifest_path, json.dumps(manifest, indent=2, default=str) + "\n")
