"""The CRM script's deterministic guarantees, tested against the thing that runs.

The predecessor of this file tested a capability that could never execute — it
imported the ``.py`` directly and called ``_runtime.configure(workspace=tmp_path)``,
which is the one environment the capability never got in production. Green for
months over a tool that failed on every real invocation.

This script has no such gap: the agent invokes it through the ``bash`` builtin,
with the workspace as cwd, and that is exactly what these tests do. What is
asserted here is what the model is therefore never asked to re-derive — slug
stability, append-never-rewrite, link rendering, and last-value-wins.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "blueprints"
    / "sales-exec-assistant"
    / "skills"
    / "crm"
    / "scripts"
    / "crm.py"
)


@pytest.fixture(scope="module")
def crm() -> ModuleType:
    """Load the shipped script exactly as it sits in the skill folder."""
    spec = importlib.util.spec_from_file_location("crm_script", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(crm: ModuleType, workspace: Path, *argv: str) -> int:
    return crm.main(["--workspace", str(workspace), *argv])


def test_the_slug_is_stable_across_spellings(crm: ModuleType) -> None:
    """One entity must always produce one slug, or its history forks in two."""
    assert crm.slugify("3G Lighting") == "3g-lighting"
    assert crm.slugify("  3G   Lighting  ") == "3g-lighting"
    assert crm.slugify("3G-Lighting!") == "3g-lighting"
    assert crm.slugify("") == "unnamed"


def test_a_logged_deal_writes_the_card_format_the_store_is_read_back_in(
    crm: ModuleType, tmp_path: Path
) -> None:
    """The header, the timestamp heading, and linked fields — the whole contract."""
    _run(
        crm,
        tmp_path,
        "log",
        "deal",
        "--name",
        "3G Lighting — scheduling/MRP opportunity",
        "--company",
        "3G Lighting",
        "--stage",
        "Procurement",
        "--next-step",
        "Security review",
        "--champion",
        "Helen Li",
    )

    card = tmp_path / "crm" / "deals" / "3g-lighting-scheduling-mrp-opportunity.md"
    text = card.read_text(encoding="utf-8")

    assert text.startswith("# 3G Lighting — scheduling/MRP opportunity\n\n")
    assert "- slug: 3g-lighting-scheduling-mrp-opportunity\n" in text
    assert "- type: deals\n" in text
    # Link fields are rendered as links; plain fields are not.
    assert "- company: [[3g-lighting]]\n" in text
    assert "- champion: [[helen-li]]\n" in text
    assert "- stage: Procurement\n" in text
    assert re.search(r"^## \d{4}-\d{2}-\d{2} \d{2}:\d{2}$", text, re.MULTILINE)


def test_logging_twice_appends_and_never_rewrites(crm: ModuleType, tmp_path: Path) -> None:
    """History is the product. A second log must not overwrite the first."""
    _run(crm, tmp_path, "log", "deal", "--name", "Acme", "--stage", "Discovery")
    _run(crm, tmp_path, "log", "deal", "--name", "Acme", "--stage", "Procurement")

    text = (tmp_path / "crm" / "deals" / "acme.md").read_text(encoding="utf-8")
    assert text.count("## ") == 2
    assert "- stage: Discovery\n" in text, "the first entry was lost"
    assert "- stage: Procurement\n" in text


def test_the_pipeline_reports_the_latest_value_not_the_first(
    crm: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reading the first entry reports a stage the deal left months ago."""
    _run(
        crm,
        tmp_path,
        "log",
        "deal",
        "--name",
        "Acme",
        "--stage",
        "Discovery",
        "--next-step",
        "Demo",
    )
    _run(
        crm,
        tmp_path,
        "log",
        "deal",
        "--name",
        "Acme",
        "--stage",
        "Procurement",
        "--next-step",
        "Security review",
    )
    capsys.readouterr()

    _run(crm, tmp_path, "pipeline")
    out = capsys.readouterr().out

    assert "- acme: [Procurement] next: Security review" in out
    assert "Discovery" not in out


def test_a_deal_with_no_next_step_is_flagged_out_loud(
    crm: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No scheduled next step is the strongest slip predictor — never log it quietly."""
    _run(crm, tmp_path, "log", "deal", "--name", "Northwind", "--stage", "Discovery")

    assert "⚠ no next step set" in capsys.readouterr().out


def test_the_pipeline_marks_a_deal_that_has_no_next_step(
    crm: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(crm, tmp_path, "log", "deal", "--name", "Northwind", "--stage", "Discovery")
    capsys.readouterr()

    _run(crm, tmp_path, "pipeline")
    assert "next: no next step ⚠" in capsys.readouterr().out


def test_commitments_share_one_card(crm: ModuleType, tmp_path: Path) -> None:
    """Commitments are a running log, not one card per promise."""
    _run(crm, tmp_path, "log", "commitment", "--commitment", "Send pricing", "--owner", "Josh")
    _run(
        crm,
        tmp_path,
        "log",
        "commitment",
        "--commitment",
        "Return the redlines",
        "--owner",
        "Legal",
    )

    cards = list((tmp_path / "crm" / "commitments").glob("*.md"))
    assert [card.name for card in cards] == ["commitments.md"]
    assert cards[0].read_text(encoding="utf-8").count("## ") == 2


def test_an_empty_field_is_omitted_rather_than_written_blank(
    crm: ModuleType, tmp_path: Path
) -> None:
    """A blank value is not knowledge; writing one makes the card lie."""
    _run(crm, tmp_path, "log", "contact", "--name", "Helen Li")

    text = (tmp_path / "crm" / "contacts" / "helen-li.md").read_text(encoding="utf-8")
    assert "- name: Helen Li\n" in text
    assert "- role:" not in text
    assert "- email:" not in text


def test_an_unknown_field_is_refused_rather_than_silently_dropped(
    crm: ModuleType, tmp_path: Path
) -> None:
    """The schema is enforced here so a mistyped field cannot write a bad card."""
    with pytest.raises(SystemExit):
        _run(crm, tmp_path, "log", "deal", "--name", "Acme", "--stagge", "Discovery")


def test_the_pipeline_is_honest_about_an_empty_store(
    crm: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(crm, tmp_path, "pipeline")
    assert capsys.readouterr().out.strip() == "No deals logged yet."
