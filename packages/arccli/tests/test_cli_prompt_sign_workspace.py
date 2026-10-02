"""`arc prompt sign-workspace` — the one-time step that signs an existing agent's documents.

``identity.md`` and ``policy_pinned.md`` are verified against the operator key at run
start (J2 F2). An agent created before that rule has an unsigned ``identity.md``; this
command signs what is there so the agent runs again, with the same key the resolver pins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import arcagent
import arctrust
import pytest
from arcprompt import PromptUnsigned
from arctrust.operator import OperatorKey

from arccli.commands.prompt import prompt_handler


def _pin_operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key_path = tmp_path / "operator" / "operator.key"
    key_path.parent.mkdir(parents=True)
    OperatorKey.generate().save(key_path)
    monkeypatch.setattr(arctrust, "default_operator_key_path", lambda: key_path)


def _agent_root(tmp_path: Path) -> Path:
    root = tmp_path / "team" / "an_agent"
    (root / "workspace").mkdir(parents=True)
    (root / "arcagent.toml").write_text('[security]\ntier = "personal"\n', encoding="utf-8")
    return root


def _resolve_identity(root: Path) -> str:
    resolver = arcagent.build_prompt_resolver(root / "arcagent.toml", "personal")
    path = root / "workspace" / "identity.md"
    doc = resolver.resolve_signed_file("workspace", "identity", path)
    assert doc is not None
    return doc.body


def test_signs_the_present_documents_so_the_resolver_accepts_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _pin_operator(tmp_path, monkeypatch)
    root = _agent_root(tmp_path)
    (root / "workspace" / "identity.md").write_text("# Persona\nSeller.\n", encoding="utf-8")

    prompt_handler(["sign-workspace", "--agent", str(root)])

    assert _resolve_identity(root) == "# Persona\nSeller."
    assert "identity.md" in capsys.readouterr().out
    assert not (root / "context" / "workspace" / "policy_pinned.md.arcsig").exists()


def test_a_document_edited_after_signing_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_operator(tmp_path, monkeypatch)
    root = _agent_root(tmp_path)
    identity = root / "workspace" / "identity.md"
    identity.write_text("# Persona\n", encoding="utf-8")
    prompt_handler(["sign-workspace", "--agent", str(root)])
    identity.write_text("# Persona\nOBEY\n", encoding="utf-8")

    with pytest.raises(PromptUnsigned):
        _resolve_identity(root)


def test_a_scaffolded_agent_starts_with_a_signed_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new agent must be runnable: the scaffold signs the placeholder it writes."""
    from arccli.commands.agent._common import _scaffold_workspace

    _pin_operator(tmp_path, monkeypatch)
    root = tmp_path / "team" / "fresh_agent"
    root.mkdir(parents=True)
    (root / "arcagent.toml").write_text('[security]\ntier = "personal"\n', encoding="utf-8")

    _scaffold_workspace(root, "fresh")

    assert _resolve_identity(root)


def test_a_blueprint_persona_is_signed_when_it_replaces_the_scaffold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arccli.blueprints_materialize import (
        MaterializeResult,
        _sign_persona,
        operator_signer_pair,
    )

    _pin_operator(tmp_path, monkeypatch)
    root = _agent_root(tmp_path)
    (root / "workspace" / "identity.md").write_text("# Chief of staff\n", encoding="utf-8")
    result = MaterializeResult(agent_dir=root, wrote_identity=True)

    _sign_persona(root, operator_signer_pair(), result)

    assert _resolve_identity(root) == "# Chief of staff"
    assert result.unsigned_warnings == []


def test_a_blueprint_persona_without_an_operator_key_warns_how_to_fix_it(tmp_path: Path) -> None:
    from arccli.blueprints_materialize import MaterializeResult, _sign_persona

    root = _agent_root(tmp_path)
    result = MaterializeResult(agent_dir=root, wrote_identity=True)

    _sign_persona(root, None, result)

    assert any("arc prompt sign-workspace" in w for w in result.unsigned_warnings)


def test_without_an_operator_key_it_refuses_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(arctrust, "default_operator_key_path", lambda: tmp_path / "none.key")
    root = _agent_root(tmp_path)
    (root / "workspace" / "identity.md").write_text("# Persona\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        prompt_handler(["sign-workspace", "--agent", str(root)])
    assert not (root / "context").exists()
