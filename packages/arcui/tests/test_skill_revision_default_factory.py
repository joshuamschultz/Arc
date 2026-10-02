"""`arcui.serve()` builds the same zero-config skill revision authority as `arc ui start`."""

from __future__ import annotations

from pathlib import Path

from arctrust import FileJournalAnchor, OperatorKey, config_file, default_operator_key_path

from arcui.routes.trust import default_skill_revision_anchor_factory

_DID = "did:arc:agent:ada"


def _tier(tier: str) -> None:
    path = config_file("arcagent.toml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[security]\ntier = "{tier}"\n', encoding="utf-8")


def test_personal_default_is_the_operator_signed_local_journal(tmp_path: Path) -> None:
    OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    factory = default_skill_revision_anchor_factory()
    assert factory is not None
    assert isinstance(factory(_DID, "reporter"), FileJournalAnchor)


def test_without_an_operator_key_the_authority_fails_closed(tmp_path: Path) -> None:
    assert default_skill_revision_anchor_factory() is None


def test_federal_without_an_external_anchor_fails_closed(tmp_path: Path) -> None:
    _tier("federal")
    assert default_skill_revision_anchor_factory() is None
