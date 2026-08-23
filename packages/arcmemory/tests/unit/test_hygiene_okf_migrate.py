"""okf_migrate_workspace — pre-OKF memory files re-rendered by the canonical writer.

``mdfile`` reads every memory document through the strict OKF parser, so a
file written before OKF (frontmatter without ``type``, wiki links) fails
closed everywhere it is read. Migration is data-side: re-render through
``render_document`` — stamping ``type``, normalising links — never loosen
the parser.
"""

from __future__ import annotations

from pathlib import Path

from arcokf import lint

from arcmemory.hygiene import okf_migrate_workspace

_LEGACY = """---
slug: acme-corp
classification: UNCLASSIFIED
---
# Acme Corp

A customer. See [[other-entity]] for the parent.
"""

_VALID = """---
type: ArcMemory
slug: fine-corp
---
# Fine Corp

Already canonical.
"""


def _seed(workspace: Path) -> tuple[Path, Path]:
    ents = workspace / "memory" / "entities"
    ents.mkdir(parents=True)
    legacy = ents / "acme-corp.md"
    legacy.write_text(_LEGACY, encoding="utf-8")
    valid = ents / "fine-corp.md"
    valid.write_text(_VALID, encoding="utf-8")
    return legacy, valid


def test_dry_run_reports_pending_files_and_writes_nothing(tmp_path: Path) -> None:
    legacy, _ = _seed(tmp_path)
    before = legacy.read_text(encoding="utf-8")

    report = okf_migrate_workspace(tmp_path, apply=False)

    assert [p.name for p in report.migrated] == ["acme-corp.md"]
    assert report.already_valid == 1
    assert report.failed == ()
    assert legacy.read_text(encoding="utf-8") == before


def test_apply_rewrites_to_valid_okf_and_is_idempotent(tmp_path: Path) -> None:
    legacy, _ = _seed(tmp_path)

    report = okf_migrate_workspace(tmp_path, apply=True)

    assert [p.name for p in report.migrated] == ["acme-corp.md"]
    assert lint(legacy).valid
    text = legacy.read_text(encoding="utf-8")
    assert "type: ArcMemory" in text
    assert "slug: acme-corp" in text
    assert "Acme Corp" in text

    again = okf_migrate_workspace(tmp_path, apply=True)
    assert again.migrated == ()
    assert again.already_valid == 2


def test_an_unparseable_file_is_reported_and_left_untouched(tmp_path: Path) -> None:
    _seed(tmp_path)
    broken = tmp_path / "memory" / "entities" / "broken.md"
    broken.write_text("---\n: not yaml at all: [\n---\nbody\n", encoding="utf-8")
    before = broken.read_text(encoding="utf-8")

    report = okf_migrate_workspace(tmp_path, apply=True)

    assert [p.name for p, _ in report.failed] == ["broken.md"]
    assert broken.read_text(encoding="utf-8") == before
