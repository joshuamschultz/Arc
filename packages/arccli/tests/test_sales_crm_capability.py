"""The shipped sales CRM capability writes correct, interlinked workspace cards.

Loads ``blueprints/sales-exec-assistant/capabilities/crm.py`` (the artifact the agent
runs) and exercises the verbs against a configured runtime + tmp workspace, closing the
coverage gap on the card-writing logic (slugging, append, `[[slug]]` links, pipeline read).
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from arcagent.builtins.capabilities import _runtime

_CRM_PY = (
    Path(__file__).resolve().parents[3]
    / "blueprints"
    / "sales-exec-assistant"
    / "capabilities"
    / "crm.py"
)


def _load_crm() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sales_crm", _CRM_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def crm(tmp_path: Path) -> ModuleType:
    _runtime.reset()
    _runtime.configure(workspace=tmp_path)
    return _load_crm()


def test_slug_and_link_are_stable(crm: ModuleType) -> None:
    assert crm._slug("Acme Corp") == "acme-corp"
    assert crm._slug("  Northwind, Inc. ") == "northwind-inc"
    assert crm._slug("") == "unnamed"
    assert crm._link("Jane Doe") == "[[jane-doe]]"
    assert crm._link("") == ""


def test_log_deal_writes_linked_card(crm: ModuleType, tmp_path: Path) -> None:
    msg = asyncio.run(
        crm.crm_log_deal(
            "Acme Renewal 2026",
            company="Acme Corp",
            stage="procurement",
            champion="Jane Doe",
        )
    )
    assert "procurement" in msg
    card = tmp_path / "crm" / "deals" / "acme-renewal-2026.md"
    text = card.read_text()
    assert "[[acme-corp]]" in text  # linked to its company
    assert "[[jane-doe]]" in text  # linked to its champion


def test_deal_without_next_step_is_flagged(crm: ModuleType) -> None:
    msg = asyncio.run(crm.crm_log_deal("Globex POC", stage="POC"))
    assert "no next step" in msg


def test_pipeline_reads_latest_stage_and_next_step(crm: ModuleType) -> None:
    asyncio.run(crm.crm_log_deal("Acme Renewal", stage="discovery", next_step="scope call"))
    asyncio.run(crm.crm_log_deal("Acme Renewal", stage="procurement", next_step="send MSA"))
    summary = asyncio.run(crm.crm_pipeline())
    # _last_field must return the MOST RECENT stage/next_step, not the first.
    assert "[procurement]" in summary
    assert "send MSA" in summary
    assert "discovery" not in summary


def test_commitment_appends_to_deal_and_log(crm: ModuleType, tmp_path: Path) -> None:
    asyncio.run(crm.crm_note_commitment("send pricing", who="me", due="2026-08-05", deal="Acme"))
    assert (tmp_path / "crm" / "commitments" / "commitments.md").is_file()
    deal_card = (tmp_path / "crm" / "deals" / "acme.md").read_text()
    assert "send pricing" in deal_card
