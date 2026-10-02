"""Zero-config skill revision authority on every production entry point (alpha-2 P5).

Before this, `arc ui start` built the dashboard without a revision anchor, so
the skill Versions tab, body, diff, rollback and edit all answered 503
"External skill revision authority is unavailable". These tests build the app
the way `arc ui start` does and drive the routes.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import arcagent
import pytest
from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.capabilities.capability_signing import sign
from arcstore.backends.memory import FakeBackend
from arctrust import FileJournalAnchor, config_file, skill_revision_anchor_dir
from starlette.testclient import TestClient

from arccli.commands import _serve
from arccli.commands.operator import resolve_operator_signer

_DID = "did:arc:agent:ada"


def _body(step: str) -> bytes:
    return (
        "---\nname: reporter\ndescription: Create reports\n---\n"
        "## Resources\nnone\n## Contract\nfollow the steps\n"
        "## Knowledge\nsource data\n## Steps\n"
        f"{step}\n"
        "## Anti Patterns\nnone\n## Examples\nexample\n## Validation\ncheck\n"
    ).encode()


@pytest.fixture(autouse=True)
def _isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Own Arc home per test, even when run from the cross-package battery
    (which shares one home and does not load this package's conftest)."""
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))


def _deployment_tier(tier: str, extra: str = "") -> None:
    path = config_file("arcagent.toml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[security]\ntier = "{tier}"\n{extra}', encoding="utf-8")


# ---------------------------------------------------------------------------
# The factory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", [None, "personal"])
def test_personal_factory_is_an_operator_signed_local_journal(
    tier: str | None, tmp_path: Path
) -> None:
    if tier is not None:
        _deployment_tier(tier)
    factory = _serve.build_skill_revision_anchor_factory()
    assert factory is not None
    anchor = factory(_DID, "reporter")
    assert isinstance(anchor, FileJournalAnchor)
    assert anchor.scope == arcagent.skill_revision_scope(_DID, "reporter")
    anchor.compare_and_advance(None, "a" * 64, "first")
    journals = list(skill_revision_anchor_dir().glob("*.jsonl"))
    assert len(journals) == 1


def test_enterprise_factory_is_the_local_journal(tmp_path: Path) -> None:
    _deployment_tier("enterprise", 'custody = "in_process"\n')
    factory = _serve.build_skill_revision_anchor_factory()
    assert factory is not None
    assert isinstance(factory(_DID, "reporter"), FileJournalAnchor)


def test_federal_without_an_external_anchor_fails_closed(tmp_path: Path) -> None:
    _deployment_tier("federal")
    assert _serve.build_skill_revision_anchor_factory() is None


def test_federal_with_an_explicit_file_anchor_fails_closed(tmp_path: Path) -> None:
    _deployment_tier("federal", 'skill_revision_anchor = "file"\n')
    assert _serve.build_skill_revision_anchor_factory() is None


def test_local_anchor_use_is_audit_warned(tmp_path: Path) -> None:
    events: list[Any] = []
    sink = SimpleNamespace(write=events.append)
    factory = _serve.build_skill_revision_anchor_factory(audit_sink=sink)
    assert factory is not None
    factory(_DID, "reporter")
    factory(_DID, "other")
    warned = [e for e in events if e.action == "revision_anchor.local"]
    assert len(warned) == 1
    assert warned[0].outcome == "warn"
    assert warned[0].extra["custody"] == "local_file"


# ---------------------------------------------------------------------------
# `arc ui start` wiring
# ---------------------------------------------------------------------------


def _ui_args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 18421,
        "viewer_token": "viewer",
        "operator_token": "operator",
        "max_agents": 10,
        "show_tokens": False,
        "root": None,
        "no_browser": True,
        "no_chat": True,
        "team_root": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _start_ui_capturing_app(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, Any]]:
    import arcui

    from arccli.commands.ui import _start

    monkeypatch.setenv("ARCSTORE_DATABASE_URL", "postgresql://arc:test@127.0.0.1/arc")
    real_create_app = arcui.create_app
    captured: dict[str, Any] = {}

    def _create_app(**kwargs: Any) -> Any:
        captured.update(kwargs)
        captured["app"] = real_create_app(**kwargs, arcstore_backend=FakeBackend())
        return captured["app"]

    monkeypatch.setattr(arcui, "create_app", _create_app)

    class _NoServe:
        def __init__(self, config: Any) -> None:
            self.app = config.app

        def run(self) -> None:
            return None

    with patch("uvicorn.Server", _NoServe):
        _start(_ui_args())
    return captured["app"], captured


def test_arc_ui_start_wires_the_skill_revision_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    app, kwargs = _start_ui_capturing_app(monkeypatch)
    factory = kwargs.get("skill_revision_anchor_factory")
    assert factory is not None
    assert app.state.skill_revision_anchor_factory is factory
    assert isinstance(factory(_DID, "reporter"), FileJournalAnchor)


def test_arc_ui_start_at_federal_without_vault_leaves_routes_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deployment_tier("federal")
    app, kwargs = _start_ui_capturing_app(monkeypatch)
    assert kwargs.get("skill_revision_anchor_factory") is None
    assert app.state.skill_revision_anchor_factory is None


# ---------------------------------------------------------------------------
# Routes on the app `arc ui start` builds
# ---------------------------------------------------------------------------


class _LiveAgent:
    def __init__(self, loader: CapabilityLoader, registry: CapabilityRegistry) -> None:
        self.loader = loader
        self.registry = registry
        self.registered_tools: list[object] = []

    @property
    def skills(self) -> list[object]:
        return list(self.registry.skill_entries())

    async def reload_or_raise(self) -> None:
        await self.loader.reload()


def _agent(tmp_path: Path, tier: str, *, signed: bool = True) -> tuple[Path, Path]:
    root = tmp_path / "team" / "ada"
    folder = root / "capabilities" / "skills" / "reporter"
    folder.mkdir(parents=True)
    (root / "workspace").mkdir()
    config = root / "arcagent.toml"
    config.write_text(f'[agent]\nname = "ada"\n\n[security]\ntier = "{tier}"\n')
    (folder / "SKILL.md").write_bytes(_body("v1"))
    if not signed:
        return root, folder
    sign(
        folder / "SKILL.md",
        signer_did="did:arc:operator:test",
        signer=resolve_operator_signer(),
        config_path=config,
    )
    return root, folder


def _attach_agent(app: Any, root: Path, folder: Path) -> _LiveAgent:
    resolver = arcagent.AnchoredSkillRevisionResolver(
        agent_did=_DID,
        config_path=root / "arcagent.toml",
        anchor_factory=app.state.skill_revision_anchor_factory,
    )
    registry = CapabilityRegistry()
    loader = CapabilityLoader(
        scan_roots=[("agent-skills", folder.parent)],
        registry=registry,
        skill_artifact_resolver=resolver,
        require_signature=True,
        trusted_public_keys=(resolve_operator_signer().public_key,),
    )
    live = _LiveAgent(loader, registry)
    app.state.roster_provider = lambda: [
        SimpleNamespace(agent_id="ada", did=_DID, workspace_path=root)
    ]
    app.state.embedded_agent_cache = {_DID: live}
    return live


def test_versions_body_diff_rollback_work_on_a_zero_config_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = _start_ui_capturing_app(monkeypatch)
    root, folder = _agent(tmp_path, "personal")
    _attach_agent(app, root, folder)
    viewer = {"Authorization": "Bearer viewer"}
    operator = {"Authorization": "Bearer operator"}
    path = "/api/agents/ada/skills/reporter"
    with TestClient(app) as client:
        unenrolled = client.get(path + "/versions", headers=viewer)
        assert unenrolled.status_code == 200, unenrolled.text
        assert unenrolled.json()["items"] == []
        detail = client.get(path + "/detail", headers=operator)
        assert detail.status_code == 200, detail.text
        assert detail.json()["content"] == _body("v1").decode()
        first = client.put(
            path + "/revision",
            headers=operator,
            json={"content": _body("v2").decode(), "expected_sha256": detail.json()["sha256"]},
        )
        assert first.status_code == 200, first.text
        second = client.put(
            path + "/revision",
            headers=operator,
            json={
                "content": _body("v3").decode(),
                "expected_sha256": hashlib.sha256(_body("v2")).hexdigest(),
            },
        )
        assert second.status_code == 200, second.text
        versions = client.get(path + "/versions", headers=viewer)
        assert versions.status_code == 200, versions.text
        items = versions.json()["items"]
        assert [row["generation"] for row in items] == [2, 1]
        newest, oldest = items[0]["candidate_id"], items[1]["candidate_id"]
        body = client.get(path + f"/versions/{oldest}/body", headers=viewer)
        assert body.status_code == 200, body.text
        assert body.json()["body"] == _body("v2").decode()
        diff = client.get(path + f"/versions/diff?a={oldest}&b={newest}", headers=viewer)
        assert diff.status_code == 200, diff.text
        assert "+v3" in diff.json()["diff"]
        rollback = client.post(
            path + "/rollback", headers=operator, json={"candidate_id": oldest, "confirm": True}
        )
        assert rollback.status_code == 200, rollback.text
        after = client.get(path + "/detail", headers=viewer)
        assert after.json()["content"] == _body("v2").decode()
        assert [
            row["generation"]
            for row in client.get(path + "/versions", headers=viewer).json()["items"]
        ] == [3, 2, 1]
    assert list(skill_revision_anchor_dir().glob("*.jsonl"))


def test_routes_fail_closed_at_federal_without_an_external_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deployment_tier("federal")
    app, _ = _start_ui_capturing_app(monkeypatch)
    root, _folder = _agent(tmp_path, "federal", signed=False)
    app.state.roster_provider = lambda: [
        SimpleNamespace(agent_id="ada", did=_DID, workspace_path=root)
    ]
    with TestClient(app) as client:
        versions = client.get(
            "/api/agents/ada/skills/reporter/versions", headers={"Authorization": "Bearer viewer"}
        )
    assert versions.status_code == 503
    assert versions.json()["error"] == "External skill revision authority is unavailable"
