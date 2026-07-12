"""Tests for ``arc memory dedup`` — merge pre-canonicalization duplicate cards.

Before slug canonicalization landed, the distiller wrote entity/procedure/insight
cards under free-text slugs, so one real thing became several files ("Custom
ERP.md" + "custom-erp.md"). ``arc memory dedup`` groups the variants by their
canonical slug, merges each group into the single canonical file, and deletes the
variants. Dry-run by default; ``--apply`` writes. It reuses
``arcmemory.canonical_slug`` so the CLI and the store agree exactly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arccli.commands.memory import memory_handler

# ---------------------------------------------------------------------------
# fixture — a workspace with one duplicate group per store
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_workspace(workspace: Path) -> None:
    """Create memory/{entities,procedures,insights} each with a canonical-vs-variant dupe."""
    ents = workspace / "memory" / "entities"
    # Two files that both canonicalize to "custom-erp".
    _write(
        ents / "Custom ERP.md",
        "---\n"
        "entity_type: system\n"
        "entity_id: custom-erp\n"
        "name: Custom ERP\n"
        "classification: unclassified\n"
        "cross_session_visibility: false\n"
        "confidence: 0.9\n"
        "links_to: []\n"
        "tags: [erp]\n"
        "---\n\n"
        "# Custom ERP\n\n## Facts\n- vendor: Acme .9 2024-01-01\n",
    )
    _write(
        ents / "custom-erp.md",
        "---\n"
        "entity_type: unknown\n"
        "entity_id: custom-erp\n"
        "name: ''\n"
        "classification: unclassified\n"
        "cross_session_visibility: false\n"
        "confidence: 0.8\n"
        "links_to: []\n"
        "tags: [system]\n"
        "---\n\n"
        "# custom-erp\n\n## Facts\n- users: 200 .8 2024-02-01\n",
    )

    procs = workspace / "memory" / "procedures"
    _write(
        procs / "Deploy Agent.md",
        "---\n"
        "slug: deploy-agent\n"
        "title: Deploy Agent\n"
        "when_to_use: when deploying\n"
        "use_count: 5\n"
        "classification: unclassified\n"
        "---\n\n"
        "# Deploy Agent\n\n## Steps\n1. only step\n",
    )
    _write(
        procs / "deploy-agent.md",
        "---\n"
        "slug: deploy-agent\n"
        "title: Deploy Agent\n"
        "when_to_use: when deploying\n"
        "use_count: 3\n"
        "classification: unclassified\n"
        "---\n\n"
        "# Deploy Agent\n\n## Steps\n1. step one\n2. step two\n",
    )

    ins = workspace / "memory" / "insights"
    _write(
        ins / "Big Idea.md",
        "---\n"
        "id: big-idea\n"
        "trigger: some trigger\n"
        "cues: [b, c]\n"
        "instances: [ep2]\n"
        "classification: unclassified\n"
        "confidence: 0.9\n"
        "salience: 0.7\n"
        "status: known\n"
        "hits: 5\n"
        "---\n\n"
        "# Big Idea\n\n## Statement\nBetter statement.\n",
    )
    _write(
        ins / "big-idea.md",
        "---\n"
        "id: big-idea\n"
        "trigger: some trigger\n"
        "cues: [a, b]\n"
        "instances: [ep1]\n"
        "classification: unclassified\n"
        "confidence: 0.6\n"
        "salience: 0.5\n"
        "status: guessed\n"
        "hits: 2\n"
        "---\n\n"
        "# big-idea\n\n## Statement\nThe insight statement.\n",
    )


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------


def test_dry_run_reports_groups_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    before = {p: p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.md")}

    memory_handler(["dedup", str(tmp_path)])
    out = capsys.readouterr().out

    # Reports one group per store and the canonical target.
    assert "entities: 1 group(s)" in out
    assert "procedures: 1 group(s)" in out
    assert "insights: 1 group(s)" in out
    assert "-> custom-erp.md" in out
    assert "dry-run" in out
    assert "3 duplicate group(s) to merge" in out

    # Nothing on disk changed.
    after = {p: p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.md")}
    assert after == before


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def test_apply_merges_into_canonical_and_deletes_variants(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    memory_handler(["dedup", "--apply", str(tmp_path)])

    ents = tmp_path / "memory" / "entities"
    procs = tmp_path / "memory" / "procedures"
    ins = tmp_path / "memory" / "insights"

    # Variants deleted; only the canonical file remains per store.
    assert not (ents / "Custom ERP.md").exists()
    assert not (procs / "Deploy Agent.md").exists()
    assert not (ins / "Big Idea.md").exists()
    assert sorted(p.name for p in ents.glob("*.md")) == ["custom-erp.md"]
    assert sorted(p.name for p in procs.glob("*.md")) == ["deploy-agent.md"]
    assert sorted(p.name for p in ins.glob("*.md")) == ["big-idea.md"]

    # Entity: facts unioned, richest name/type win.
    entity_text = (ents / "custom-erp.md").read_text(encoding="utf-8")
    assert "vendor: Acme" in entity_text
    assert "users: 200" in entity_text
    assert "name: Custom ERP" in entity_text
    assert "entity_type: system" in entity_text

    # Procedure: richest steps kept, use_count summed (5 + 3).
    proc_text = (procs / "deploy-agent.md").read_text(encoding="utf-8")
    assert "step one" in proc_text
    assert "step two" in proc_text
    assert "use_count: 8" in proc_text

    # Insight: cues/instances unioned, max confidence/hits.
    insight_text = (ins / "big-idea.md").read_text(encoding="utf-8")
    for cue in ("a", "b", "c"):
        assert f"- {cue}\n" in insight_text
    assert "ep1" in insight_text and "ep2" in insight_text
    assert "confidence: 0.9" in insight_text
    assert "hits: 5" in insight_text


def test_second_apply_is_a_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    memory_handler(["dedup", "--apply", str(tmp_path)])
    capsys.readouterr()  # drop first-run output

    memory_handler(["dedup", "--apply", str(tmp_path)])
    out = capsys.readouterr().out
    assert "0 duplicate group(s) merged" in out
    assert "no duplicates." in out


# ---------------------------------------------------------------------------
# discovery + dispatch
# ---------------------------------------------------------------------------


def test_root_discovers_nested_workspaces(tmp_path: Path) -> None:
    """A root that is not itself a workspace is searched for nested ones."""
    agent = tmp_path / "agents" / "alice" / "workspace"
    _seed_workspace(agent)
    memory_handler(["dedup", "--apply", str(tmp_path)])
    assert not (agent / "memory" / "entities" / "Custom ERP.md").exists()


def test_missing_workspace_reports_and_continues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    memory_handler(["dedup", str(tmp_path / "does-not-exist")])
    err = capsys.readouterr().err
    assert "no memory workspace found" in err


def test_no_subcommand_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        memory_handler([])
    out = capsys.readouterr().out
    assert "memory" in out.lower()


def test_memory_registered_in_command_registry() -> None:
    from arccli.commands.registry import resolve_command

    cmd = resolve_command("memory")
    assert cmd is not None
    assert cmd.handler is not None
