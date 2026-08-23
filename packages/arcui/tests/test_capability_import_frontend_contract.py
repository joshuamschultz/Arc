"""Source-level contract for the capability import UI until browser tests land."""

from pathlib import Path

_WEB = Path(__file__).resolve().parents[1] / "web" / "src"


def _read(relative: str) -> str:
    return (_WEB / relative).read_text(encoding="utf-8")


def test_capability_import_ui_is_agent_scoped_and_review_only() -> None:
    panel = _read("components/capability-import-panel.tsx")
    hook = _read("hooks/use-capability-import.ts")
    assert "Target agent" in panel
    assert "onDrop" in panel
    assert "review_only" in hook
    assert "inactive" in panel
    assert "capability-imports" in hook
    assert "readFile" in hook
    assert "editFile" in hook
    assert "Save reviewed edit" in panel
    assert "Staged imports" in panel
    assert "selectReview" in hook


def test_capability_import_ui_does_not_offer_fake_activation() -> None:
    panel = _read("components/capability-import-panel.tsx")
    assert "onActivate" not in panel
    assert "apiPost" not in panel
