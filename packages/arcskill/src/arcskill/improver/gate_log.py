"""Gate log — the persisted record of every eval-gate verdict (alpha-2 P8).

The improver decides acceptance with :class:`~arcskill.improver.evalgate.EvalGate`
and the operator-approval ladder, but those verdicts used to live only in a log
line. The read model (:mod:`arcskill.improver.reader`) needs them: an operator
asks "why did my skill not change?" and the answer is the last gate verdict and
its reason.

One JSON line per improvement pass under
``<workspace>/skill_traces/<skill>/gate_log.jsonl``, bounded: when the file grows
past :data:`_MAX_LINES` it is rewritten atomically with the newest
:data:`_KEEP_LINES` entries. The log is operational state, not the audit trail —
the tamper-evident record stays on the WORM chain the improver emits to.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcskill.improver._util import atomic_write_text
from arcskill.improver.candidate_store import _validate_skill_name

_logger = logging.getLogger("arcskill.improver.gate_log")

_FILE_NAME = "gate_log.jsonl"
_MAX_LINES = 500
_KEEP_LINES = 200


@dataclass(frozen=True)
class GateRecord:
    """One improvement pass: the gate verdict, its reason, and what happened next.

    ``source`` is ``auto`` (the usage-triggered pass) or ``manual`` (an operator's
    improve-now). ``outcome`` is ``applied``, ``rejected`` (the gate said no) or
    ``denied`` (the gate said yes but authorization refused).
    """

    skill_name: str
    source: str
    kind: str
    accepted: bool
    reason: str
    outcome: str
    candidate_id: str = ""
    before_pass: int = 0
    after_pass: int = 0
    newly_passing: int = 0
    ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GateLog:
    """Append and read the per-skill gate verdict log (bounded JSONL)."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    def path(self, skill_name: str) -> Path:
        _validate_skill_name(skill_name)
        return self._workspace / "skill_traces" / skill_name / _FILE_NAME

    def record(self, record: GateRecord) -> None:
        """Append one verdict; trim the file when it passes the bound."""
        path = self.path(record.skill_name)
        stamped = record.ts or datetime.now(UTC).isoformat()
        line = json.dumps({**record.to_dict(), "ts": stamped}, sort_keys=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self._trim(path)
        except OSError:
            # Operational state only (the WORM chain carries the audit) — never let a
            # full disk turn a finished pass into a crash.
            _logger.warning("gate log for %s is not writable", record.skill_name)

    def recent(self, skill_name: str, *, limit: int = 20) -> list[GateRecord]:
        """The newest ``limit`` verdicts, newest first; unreadable lines are skipped."""
        path = self.path(skill_name)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: list[GateRecord] = []
        for raw in reversed(lines):
            parsed = _parse(raw)
            if parsed is not None:
                records.append(parsed)
            if len(records) >= limit:
                break
        return records

    @staticmethod
    def _trim(path: Path) -> None:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_LINES:
            atomic_write_text(path, "\n".join(lines[-_KEEP_LINES:]) + "\n")


def _parse(raw: str) -> GateRecord | None:
    try:
        data = json.loads(raw)
        return GateRecord(**data) if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


__all__ = ["GateLog", "GateRecord"]
