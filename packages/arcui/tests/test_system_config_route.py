"""`GET/PATCH /api/system-config/{file}` — the system-level (~/.arc) config editor.

Mirrors `test_agent_config_files_route.py` but targets the user-wide config root
(``${ARC_CONFIG_DIR:-~/.arc}``), which per-agent files layer over. Reads return
each file's top-level TOML sections; PATCH deep-merges one section and writes it
back, preserving comments and refusing a result that would not re-parse.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes import system_config as system_config_routes

_ARCLLM_TOML = (
    "# fleet arcllm config\n"
    "[defaults]\n"
    'provider = "anthropic"  # keep the comment\n'
    "temperature = 0.7\n"
    "[vault]\n"
    'backend = "env"\n'
    'signing_key = "sk-should-be-redacted-for-viewers-1234"\n'
)
_ARCRUN_TOML = "[loop]\nmax_turns = 20\n"


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    config_root = tmp_path / "arc-config"
    config_root.mkdir()
    (config_root / "arcllm.toml").write_text(_ARCLLM_TOML, encoding="utf-8")
    (config_root / "arcrun.toml").write_text(_ARCRUN_TOML, encoding="utf-8")
    monkeypatch.setenv("ARC_CONFIG_DIR", str(config_root))

    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=system_config_routes.routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    return TestClient(app), config_root


def _get(client: TestClient, file: str, token: str = "viewer"):
    return client.get(
        f"/api/system-config/{file}",
        headers={"Authorization": f"Bearer {token}"},
    )


def _patch(client: TestClient, file: str, body: dict, token: str = "operator"):
    return client.patch(
        f"/api/system-config/{file}",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_get_returns_sections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(tmp_path, monkeypatch)
    resp = _get(client, "arcrun")
    assert resp.status_code == 200
    body = resp.json()
    assert body["file"] == "arcrun"
    assert body["sections"]["loop"]["max_turns"] == 20
    assert body["mtime"] > 0


def test_operator_sees_secret_viewer_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client(tmp_path, monkeypatch)
    operator = _get(client, "arcllm", token="operator").json()
    assert operator["sections"]["vault"]["signing_key"].startswith("sk-should")
    viewer = _get(client, "arcllm", token="viewer").json()
    assert viewer["sections"]["vault"]["signing_key"] == "***"
    # A non-sensitive key is untouched.
    assert viewer["sections"]["defaults"]["provider"] == "anthropic"


def test_unknown_file_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(tmp_path, monkeypatch)
    resp = _get(client, "passwd")
    assert resp.status_code == 404


def test_missing_file_returns_empty_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, config_root = _client(tmp_path, monkeypatch)
    (config_root / "arcrun.toml").unlink()
    resp = _get(client, "arcrun")
    assert resp.status_code == 200
    assert resp.json() == {"file": "arcrun", "sections": {}, "mtime": 0.0}


def test_patch_merges_and_preserves_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, config_root = _client(tmp_path, monkeypatch)
    resp = _patch(client, "arcrun", {"loop": {"max_turns": 42}})
    assert resp.status_code == 200
    assert resp.json()["sections"]["loop"]["max_turns"] == 42

    resp2 = _patch(client, "arcllm", {"defaults": {"temperature": 0.1}})
    assert resp2.status_code == 200
    on_disk = (config_root / "arcllm.toml").read_text(encoding="utf-8")
    assert "keep the comment" in on_disk  # comment survived the round-trip
    assert "temperature = 0.1" in on_disk
    assert 'provider = "anthropic"' in on_disk  # untouched key preserved


def test_patch_viewer_is_403(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(tmp_path, monkeypatch)
    resp = _patch(client, "arcrun", {"loop": {"max_turns": 5}}, token="viewer")
    assert resp.status_code == 403


def test_patch_unknown_file_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(tmp_path, monkeypatch)
    resp = _patch(client, "passwd", {"x": {"y": 1}})
    assert resp.status_code == 404


def test_patch_missing_file_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, config_root = _client(tmp_path, monkeypatch)
    (config_root / "arcrun.toml").unlink()
    resp = _patch(client, "arcrun", {"loop": {"max_turns": 5}})
    assert resp.status_code == 404


def test_patch_non_object_body_is_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.patch(
        "/api/system-config/arcrun",
        json=[1, 2, 3],
        headers={"Authorization": "Bearer operator"},
    )
    assert resp.status_code == 400
