"""Settings -> Maintenance -> Modules: see, enable, disable and upgrade modules per agent.

The dashboard drives the same signed-bundle path ``arc module`` does. Nothing is
installed from a name alone: a bundle in the staging directory is verified at the
deployment's tier, against the operator key or a pinned issuer, before one byte is
written. A viewer can look; only an operator can change anything.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import arcagent
import arcbundle
import arctrust
import pytest
from arctrust.operator import OperatorKey
from arctrust.policy import OperatorApprovalAuthority
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

OP_TOKEN = "o" * 64
VIEW_TOKEN = "v" * 64

_CAPABILITIES = b"CAPABILITIES = []\n"


class AuditRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def audit_event(self, name: str, fields: dict[str, Any]) -> None:
        self.events.append(dict(fields))

    def outcomes(self, operation: str) -> list[str]:
        return [e["outcome"] for e in self.events if e.get("operation") == operation]


@pytest.fixture
def operator_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OperatorKey:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    path = arctrust.default_operator_key_path()
    path.parent.mkdir(parents=True)
    key = OperatorKey.generate()
    key.save(path)
    return key


@pytest.fixture
def team_root(tmp_path: Path, operator_key: OperatorKey) -> Path:
    root = tmp_path / "team"
    root.mkdir()
    for name in ("olivia", "mira"):
        arcagent.scaffold.create_agent(
            root,
            name,
            operator=arcagent.scaffold.OperatorSigning(
                did=OperatorApprovalAuthority(operator_key.into_signer()).did,
                signer=operator_key.into_signer(),
            ),
        )
    return root


@pytest.fixture
def audit() -> AuditRecorder:
    return AuditRecorder()


@pytest.fixture
def client(team_root: Path, operator_key: OperatorKey, audit: AuditRecorder) -> TestClient:
    app = create_app(
        auth_config=AuthConfig({"viewer_token": VIEW_TOKEN, "operator_token": OP_TOKEN}),
        team_root=team_root,
        operator_signer_factory=lambda: operator_key.into_signer(),
    )
    app.state.audit = audit
    return TestClient(app)


def _op() -> dict[str, str]:
    return {"Authorization": f"Bearer {OP_TOKEN}"}


def _viewer() -> dict[str, str]:
    return {"Authorization": f"Bearer {VIEW_TOKEN}"}


def _stage(
    tmp_path: Path,
    operator_key: OperatorKey,
    module: str = "demo",
    version: str = "1.0.0",
    body: bytes = _CAPABILITIES,
    issuer: str | None = None,
    signer_key: OperatorKey | None = None,
) -> Path:
    """Build a signed bundle into the deployment's staging directory."""
    source = tmp_path / f"src-{module}-{version}"
    source.mkdir()
    (source / "capabilities.py").write_bytes(body)
    (source / "_runtime.py").write_bytes(b"def configure():\n    return None\n")
    signing = signer_key or operator_key
    did = issuer or OperatorApprovalAuthority(operator_key.into_signer()).did
    store = arctrust.paths.bundles_dir()
    store.mkdir(parents=True, exist_ok=True)
    return arcbundle.build_bundle(
        source,
        module=module,
        version=version,
        private_key=signing.into_signer(),
        issuer=did,
        out=store / f"{module}.arcbundle.new-{version}",
    )


def _stage_as(bundle: Path, module: str) -> Path:
    """Move a freshly built bundle to its canonical staged name, replacing an older one."""
    import shutil

    final = bundle.with_name(f"{module}.arcbundle")
    if final.exists():
        shutil.rmtree(final)
    bundle.rename(final)
    return final


def _agent_modules(team_root: Path, name: str) -> dict[str, Any]:
    text = (team_root / name / "arcagent.toml").read_text(encoding="utf-8")
    return tomllib.loads(text).get("modules", {})


def test_a_viewer_sees_modules_with_per_agent_state(
    client: TestClient, tmp_path: Path, operator_key: OperatorKey
) -> None:
    _stage_as(_stage(tmp_path, operator_key), "demo")

    resp = client.get("/api/maintenance/modules", headers=_viewer())

    assert resp.status_code == 200
    body = resp.json()
    assert {a["agent_id"] for a in body["agents"]} == {"olivia", "mira"}
    demo = next(m for m in body["modules"] if m["name"] == "demo")
    assert demo["installed"] is False
    assert demo["staged"]["version"] == "1.0.0"
    assert demo["staged"]["update_available"] is True
    assert demo["agents"]["olivia"]["enabled"] is False


def test_an_operator_installs_the_staged_bundle_for_one_agent(
    client: TestClient, tmp_path: Path, operator_key: OperatorKey, team_root: Path, audit
) -> None:
    _stage_as(_stage(tmp_path, operator_key), "demo")

    resp = client.post(
        "/api/maintenance/modules/demo/install", json={"agent_id": "olivia"}, headers=_op()
    )

    assert resp.status_code == 200, resp.text
    assert arcagent.module_root().joinpath("demo", "capabilities.py").is_file()
    assert _agent_modules(team_root, "olivia")["demo"]["enabled"] is True
    assert "demo" not in _agent_modules(team_root, "mira")
    assert audit.outcomes("module.install") == ["applied"]
    listing = client.get("/api/maintenance/modules", headers=_viewer()).json()
    demo = next(m for m in listing["modules"] if m["name"] == "demo")
    assert demo["installed"] is True
    assert demo["staged"]["update_available"] is False
    assert demo["agents"]["olivia"]["enabled"] is True


def test_an_upgrade_installs_the_newer_staged_bundle(
    client: TestClient, tmp_path: Path, operator_key: OperatorKey
) -> None:
    _stage_as(_stage(tmp_path, operator_key), "demo")
    client.post(
        "/api/maintenance/modules/demo/install", json={"agent_id": "olivia"}, headers=_op()
    )
    _stage_as(
        _stage(tmp_path, operator_key, version="2.0.0", body=b"CAPABILITIES = [1]\n"), "demo"
    )

    listing = client.get("/api/maintenance/modules", headers=_viewer()).json()
    demo = next(m for m in listing["modules"] if m["name"] == "demo")
    assert demo["staged"]["update_available"] is True

    resp = client.post(
        "/api/maintenance/modules/demo/install", json={"agent_id": "olivia"}, headers=_op()
    )

    assert resp.status_code == 200, resp.text
    assert arcagent.module_root().joinpath("demo", "capabilities.py").read_bytes() == (
        b"CAPABILITIES = [1]\n"
    )
    assert resp.json()["restart_needed"] is True


def test_a_bundle_signed_by_a_stranger_is_refused_and_nothing_is_written(
    client: TestClient, tmp_path: Path, operator_key: OperatorKey, team_root: Path, audit
) -> None:
    stranger = OperatorKey.generate()
    _stage_as(
        _stage(tmp_path, operator_key, issuer="did:arc:stranger", signer_key=stranger), "demo"
    )

    resp = client.post(
        "/api/maintenance/modules/demo/install", json={"agent_id": "olivia"}, headers=_op()
    )

    assert resp.status_code == 409
    assert not arcagent.module_root().joinpath("demo").exists()
    assert "demo" not in _agent_modules(team_root, "olivia")
    assert "denied" in audit.outcomes("module.install")
    assert "arc " not in resp.json()["error"]


def test_a_tampered_staged_bundle_is_refused(
    client: TestClient, tmp_path: Path, operator_key: OperatorKey
) -> None:
    bundle = _stage_as(_stage(tmp_path, operator_key), "demo")
    (bundle / "files" / "capabilities.py").chmod(0o644)
    (bundle / "files" / "capabilities.py").write_bytes(b"import os\n")

    resp = client.post(
        "/api/maintenance/modules/demo/install", json={"agent_id": "olivia"}, headers=_op()
    )

    assert resp.status_code == 409
    assert not arcagent.module_root().joinpath("demo").exists()


def test_a_viewer_cannot_enable_install_or_disable_a_module(
    client: TestClient, tmp_path: Path, operator_key: OperatorKey, team_root: Path, audit
) -> None:
    _stage_as(_stage(tmp_path, operator_key), "demo")

    for action in ("enable", "disable", "install"):
        resp = client.post(
            f"/api/maintenance/modules/demo/{action}",
            json={"agent_id": "olivia"},
            headers=_viewer(),
        )
        assert resp.status_code == 403, action

    assert not arcagent.module_root().joinpath("demo").exists()
    assert "demo" not in _agent_modules(team_root, "olivia")
    assert audit.outcomes("module.enable") == ["denied"]


def test_disable_turns_a_module_off_for_one_agent_and_keeps_its_settings(
    client: TestClient, tmp_path: Path, operator_key: OperatorKey, team_root: Path, audit
) -> None:
    _stage_as(_stage(tmp_path, operator_key), "demo")
    client.post(
        "/api/maintenance/modules/demo/install", json={"agent_id": "olivia"}, headers=_op()
    )

    resp = client.post(
        "/api/maintenance/modules/demo/disable", json={"agent_id": "olivia"}, headers=_op()
    )

    assert resp.status_code == 200, resp.text
    assert _agent_modules(team_root, "olivia")["demo"]["enabled"] is False
    assert audit.outcomes("module.disable") == ["applied"]

    again = client.post(
        "/api/maintenance/modules/demo/enable", json={"agent_id": "olivia"}, headers=_op()
    )
    assert again.status_code == 200, again.text
    assert _agent_modules(team_root, "olivia")["demo"]["enabled"] is True
    assert audit.outcomes("module.enable") == ["applied"]


def test_enabling_a_module_that_is_not_installed_is_refused_in_plain_words(
    client: TestClient,
) -> None:
    resp = client.post(
        "/api/maintenance/modules/ghost/enable", json={"agent_id": "olivia"}, headers=_op()
    )

    assert resp.status_code == 404
    assert "not installed" in resp.json()["error"]


@pytest.mark.parametrize("name", ["../etc", "a/b", "..", "Demo", "demo;rm", "x" * 100])
def test_a_module_name_is_never_a_path(client: TestClient, name: str) -> None:
    resp = client.post(
        f"/api/maintenance/modules/{name}/enable", json={"agent_id": "olivia"}, headers=_op()
    )

    # A raw ``..`` is folded away by the HTTP client before it reaches a route (405).
    assert resp.status_code in (400, 404, 405)


def test_an_unknown_agent_is_refused(
    client: TestClient, tmp_path: Path, operator_key: OperatorKey
) -> None:
    _stage_as(_stage(tmp_path, operator_key), "demo")

    resp = client.post(
        "/api/maintenance/modules/demo/install", json={"agent_id": "../x"}, headers=_op()
    )

    assert resp.status_code in (400, 404)
    assert not arcagent.module_root().joinpath("demo").exists()


class _LiveAgent:
    """The two lifecycle calls a running agent offers the dashboard."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = False

    async def enable_module_persisted(self, name: str) -> str:
        if self.fail:
            raise RuntimeError("configure failed")
        self.calls.append(("enable", name))
        return "enabled"

    async def disable_module_persisted(self, name: str) -> str:
        self.calls.append(("disable", name))
        return "disabled"


def _go_live(client: TestClient, team_root: Path) -> _LiveAgent:
    did = arcagent.load_config(team_root / "olivia" / "arcagent.toml").identity.did
    live = _LiveAgent()
    client.app.state.embedded_agent_cache = {did: live}
    return live


def test_a_running_agent_is_changed_live_with_no_restart(
    client: TestClient, tmp_path: Path, operator_key: OperatorKey, team_root: Path
) -> None:
    _stage_as(_stage(tmp_path, operator_key), "demo")
    client.post(
        "/api/maintenance/modules/demo/install", json={"agent_id": "olivia"}, headers=_op()
    )
    live = _go_live(client, team_root)

    off = client.post(
        "/api/maintenance/modules/demo/disable", json={"agent_id": "olivia"}, headers=_op()
    )
    on = client.post(
        "/api/maintenance/modules/demo/enable", json={"agent_id": "olivia"}, headers=_op()
    )

    assert off.json()["live"] is True and off.json()["restart_needed"] is False
    assert on.json()["live"] is True
    assert live.calls == [("disable", "demo"), ("enable", "demo")]


def test_a_live_change_that_fails_says_nothing_changed(
    client: TestClient, tmp_path: Path, operator_key: OperatorKey, team_root: Path, audit
) -> None:
    _stage_as(_stage(tmp_path, operator_key), "demo")
    client.post(
        "/api/maintenance/modules/demo/install", json={"agent_id": "olivia"}, headers=_op()
    )
    live = _go_live(client, team_root)
    live.fail = True

    resp = client.post(
        "/api/maintenance/modules/demo/enable", json={"agent_id": "olivia"}, headers=_op()
    )

    assert resp.status_code == 503
    assert "Nothing was changed" in resp.json()["error"]
    assert "configure failed" not in resp.text
    assert "denied" in audit.outcomes("module.enable")
