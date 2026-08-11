"""Typed results for capability scanning and transactional reload."""

from __future__ import annotations

from dataclasses import dataclass, field

from arcagent.capabilities.capability_registry import CapabilityRegistry


@dataclass(frozen=True)
class CapabilityOutcome:
    kind: str
    name: str
    version: str
    description: str
    scan_root: str
    source_path: str
    status: str
    status_detail: str


@dataclass
class ReloadDelta:
    added: list[str] = field(default_factory=list)
    replaced: list[tuple[str, str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    outcomes: list[CapabilityOutcome] = field(default_factory=list)

    def render(self) -> str:
        added = self._segment(self.added)
        removed = self._segment(self.removed)
        replaced = self._render_replaced()
        count = len(self.errors)
        head = (
            f"reload: +{len(self.added)} added{added}, "
            f"~{len(self.replaced)} replaced{replaced}, "
            f"-{len(self.removed)} removed{removed}, "
            f"{count} {'error' if count == 1 else 'errors'}"
        )
        if not self.errors:
            return head
        return "\n".join([head, *(f"  - {path}: {detail}" for path, detail in self.errors)])

    @staticmethod
    def _segment(names: list[str]) -> str:
        return " (" + ", ".join(names) + ")" if names else ""

    def _render_replaced(self) -> str:
        if not self.replaced:
            return ""
        return " (" + ", ".join(f"{name} {old}→{new}" for name, old, new in self.replaced) + ")"


@dataclass(frozen=True)
class PreparedReload:
    registry: CapabilityRegistry
    delta: ReloadDelta
    known_tools: dict[str, str]
    known_skills: dict[str, str]


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    status: str = ""
    detail: str = ""


__all__ = ["CapabilityOutcome", "GateResult", "PreparedReload", "ReloadDelta"]
