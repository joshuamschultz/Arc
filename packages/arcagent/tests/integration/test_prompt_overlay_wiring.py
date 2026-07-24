"""Integration: run-start prompt resolution, provenance, and overlay override (COMP-006).

editable-system-prompts T-753/T-755: the agent builds an overlay-aware resolver
pinned to the DEPLOYMENT OPERATOR key (the key arcui signs overlays with), freezes
the prompt set once per run, emits one provenance event, and lets a signed operator
overlay override the stock prompt in the assembled system prompt. Deleting the
overlay restores stock on the next snapshot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import arctrust
from arcprompt import render_prompt
from arctrust.artifact import sign_artifact
from arctrust.operator import OperatorKey
from arctrust.policy import OperatorApprovalAuthority

from arcagent.core.prompt_context import (
    build_prompt_resolver,
    snapshot_resolver,
    snapshot_run_prompts,
)


def _operator(tmp_path: Path, monkeypatch: Any) -> tuple[OperatorKey, str]:
    """Create a deployment operator key at a temp path and pin it as the default."""
    key_path = tmp_path / "operator" / "operator.key"
    key_path.parent.mkdir(parents=True)
    op = OperatorKey.generate()
    op.save(key_path)
    monkeypatch.setattr(arctrust, "default_operator_key_path", lambda: key_path)
    did = OperatorApprovalAuthority(op.into_signer()).did
    return op, did


def _sign_overlay(
    agent_root: Path, package: str, name: str, body: str, op: OperatorKey, did: str
) -> None:
    """Author + operator-sign an overlay exactly as the arcui SigningAuthority does."""
    overlay_dir = agent_root / "context" / package
    overlay_dir.mkdir(parents=True, exist_ok=True)
    raw = render_prompt(body, name=name, description="operator override")
    (overlay_dir / f"{name}.md").write_bytes(raw)
    manifest = sign_artifact(raw, signer_did=did, private_key=op.seed)
    (overlay_dir / f"{name}.md.arcsig").write_text(manifest.to_json(), encoding="utf-8")


class _CollectingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit_event(self, event_type: str, details: dict[str, Any]) -> None:
        self.events.append((event_type, details))


def test_signed_operator_overlay_overrides_stock(tmp_path: Path, monkeypatch: Any) -> None:
    op, did = _operator(tmp_path, monkeypatch)
    agent_root = tmp_path / "team" / "an_agent"
    (agent_root / "workspace").mkdir(parents=True)
    config_path = agent_root / "arcagent.toml"
    config_path.write_text("", encoding="utf-8")

    _sign_overlay(agent_root, "arcagent", "spawn_guidance", "OVERRIDDEN spawn guidance", op, did)

    resolver = build_prompt_resolver(config_path, "federal")
    doc = resolver.resolve("arcagent", "spawn_guidance")
    assert doc.source == "overlay"
    assert doc.body == "OVERRIDDEN spawn guidance"
    assert doc.signer_did == did


def test_snapshot_emits_one_provenance_event_with_overlay_signer(
    tmp_path: Path, monkeypatch: Any
) -> None:
    op, did = _operator(tmp_path, monkeypatch)
    agent_root = tmp_path / "team" / "an_agent"
    (agent_root / "workspace").mkdir(parents=True)
    config_path = agent_root / "arcagent.toml"
    config_path.write_text("", encoding="utf-8")
    _sign_overlay(agent_root, "arcrun", "strategy_react", "OVERRIDDEN react", op, did)

    resolver = build_prompt_resolver(config_path, "federal")
    audit = _CollectingAudit()
    snap = snapshot_run_prompts(resolver, actor_did="did:arc:agent", audit_event=audit.audit_event)

    # Exactly one provenance event, tier resolved (federal), overlay signer recorded.
    assert len(audit.events) == 1
    action, details = audit.events[0]
    assert action == "prompt.snapshot"
    assert details["tier"] == "federal"
    rows = {(r["package"], r["name"]): r for r in details["prompts"]}
    react = rows[("arcrun", "strategy_react")]
    assert react["source"] == "overlay"
    assert react["signer_did"] == did
    # A non-overridden prompt is stock with no signer — never a defaulted DID.
    assert rows[("arcagent", "spawn_guidance")]["source"] == "stock"
    assert rows[("arcagent", "spawn_guidance")]["signer_did"] is None

    # The snapshot resolver reads the frozen overlay body.
    assert snapshot_resolver(snap)("arcrun", "strategy_react") == "OVERRIDDEN react"


def test_deleting_overlay_restores_stock_next_snapshot(tmp_path: Path, monkeypatch: Any) -> None:
    op, did = _operator(tmp_path, monkeypatch)
    agent_root = tmp_path / "team" / "an_agent"
    (agent_root / "workspace").mkdir(parents=True)
    config_path = agent_root / "arcagent.toml"
    config_path.write_text("", encoding="utf-8")
    _sign_overlay(agent_root, "arcagent", "spawn_guidance", "temp override", op, did)

    resolver = build_prompt_resolver(config_path, "personal")
    assert resolver.resolve("arcagent", "spawn_guidance").source == "overlay"

    (agent_root / "context" / "arcagent" / "spawn_guidance.md").unlink()
    (agent_root / "context" / "arcagent" / "spawn_guidance.md.arcsig").unlink()
    assert resolver.resolve("arcagent", "spawn_guidance").source == "stock"


def test_unpinned_operator_key_refuses_overlay_but_resolves_stock(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """No resolvable operator key → overlays fail closed, stock still resolves."""
    op, did = _operator(tmp_path, monkeypatch)
    agent_root = tmp_path / "team" / "an_agent"
    (agent_root / "workspace").mkdir(parents=True)
    config_path = agent_root / "arcagent.toml"
    config_path.write_text("", encoding="utf-8")
    _sign_overlay(agent_root, "arcagent", "spawn_guidance", "override", op, did)

    # Now make the operator key unresolvable → build_prompt_resolver pins None.
    monkeypatch.setattr(arctrust, "default_operator_key_path", lambda: tmp_path / "missing.key")
    resolver = build_prompt_resolver(config_path, "federal")

    import pytest
    from arcprompt import PromptUnsigned

    with pytest.raises(PromptUnsigned):
        resolver.resolve("arcagent", "spawn_guidance")
    # A prompt with no overlay still resolves to stock.
    assert resolver.resolve("arcagent", "skill_usage_instruction").source == "stock"
