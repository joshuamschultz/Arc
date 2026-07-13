"""WS4 — workspace dedup/merge relocated into ``arcmemory.hygiene``.

The non-lossy merge of pre-canonicalization duplicate cards (one real thing minted
under several free-text slugs) lives here now, not in arccli. ``dedup_workspace``
groups files by canonical slug, unions their content, and — when ``apply`` — writes
the single canonical file and deletes the variants. Dry-run by default.
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.hygiene import DedupReport, dedup_workspace, discover_workspaces


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed(workspace: Path) -> None:
    ents = workspace / "memory" / "entities"
    _write(
        ents / "Custom ERP.md",
        "---\nentity_type: system\nentity_id: custom-erp\nname: Custom ERP\n"
        "classification: unclassified\ncross_session_visibility: false\nconfidence: 0.9\n"
        "links_to: []\ntags: [erp]\n---\n\n# Custom ERP\n\n## Facts\n- vendor: Acme .9 2024-01-01\n",
    )
    _write(
        ents / "custom-erp.md",
        "---\nentity_type: unknown\nentity_id: custom-erp\nname: ''\n"
        "classification: unclassified\ncross_session_visibility: false\nconfidence: 0.8\n"
        "links_to: []\ntags: [system]\n---\n\n# custom-erp\n\n## Facts\n- users: 200 .8 2024-02-01\n",
    )
    procs = workspace / "memory" / "procedures"
    _write(
        procs / "Deploy Agent.md",
        "---\nslug: deploy-agent\ntitle: Deploy Agent\nwhen_to_use: when deploying\n"
        "use_count: 5\nclassification: unclassified\n---\n\n# Deploy Agent\n\n## Steps\n1. only step\n",
    )
    _write(
        procs / "deploy-agent.md",
        "---\nslug: deploy-agent\ntitle: Deploy Agent\nwhen_to_use: when deploying\n"
        "use_count: 3\nclassification: unclassified\n---\n\n"
        "# Deploy Agent\n\n## Steps\n1. step one\n2. step two\n",
    )


def test_dry_run_reports_and_writes_nothing(tmp_path: Path) -> None:
    _seed(tmp_path)
    before = {p: p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.md")}

    report = dedup_workspace(tmp_path, apply=False)

    assert isinstance(report, DedupReport)
    assert report.groups == 2  # one entity group + one procedure group
    after = {p: p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.md")}
    assert after == before  # dry-run touches nothing


def test_apply_merges_into_canonical_and_deletes_variants(tmp_path: Path) -> None:
    _seed(tmp_path)
    report = dedup_workspace(tmp_path, apply=True)

    ents = tmp_path / "memory" / "entities"
    procs = tmp_path / "memory" / "procedures"
    assert not (ents / "Custom ERP.md").exists()
    assert sorted(p.name for p in ents.glob("*.md")) == ["custom-erp.md"]

    entity_text = (ents / "custom-erp.md").read_text(encoding="utf-8")
    assert "vendor: Acme" in entity_text and "users: 200" in entity_text
    assert "name: Custom ERP" in entity_text and "entity_type: system" in entity_text

    proc_text = (procs / "deploy-agent.md").read_text(encoding="utf-8")
    assert "step one" in proc_text and "step two" in proc_text
    assert "use_count: 8" in proc_text
    assert report.groups == 2


def test_second_apply_is_a_noop(tmp_path: Path) -> None:
    _seed(tmp_path)
    dedup_workspace(tmp_path, apply=True)
    report = dedup_workspace(tmp_path, apply=True)
    assert report.groups == 0


def test_discover_workspaces_finds_nested(tmp_path: Path) -> None:
    agent = tmp_path / "agents" / "alice" / "workspace"
    _seed(agent)
    assert discover_workspaces(tmp_path) == [agent]
    # A path that is itself a workspace is returned directly.
    assert discover_workspaces(agent) == [agent]
