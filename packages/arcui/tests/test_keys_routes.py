"""SPEC-064 T-020 — ``/api/keys``: the web holds a provider key it can never show.

The assertions that earn their place here are the negative ones. A route that
stores a key correctly and also echoes it back passes every happy-path test ever
written for it, so the sentinel checks assert on the *serialized* body — the
bytes a browser receives — rather than on a dict the test assembled itself.

Harness mirrors ``test_agent_config_files_route.py``: a real Starlette app, the
real ``AuthMiddleware``, and two tokens standing in for the two roles.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcagent import default_env_file
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.keys import routes as keys_routes

#: Distinctive enough that finding it anywhere in a response is proof of a leak.
_SENTINEL = "sk-zzz-web-key-sentinel-4711"

_DECLARED = "ANTHROPIC_API_KEY"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """An app whose ``~/.arc`` is entirely inside ``tmp_path``."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=keys_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    return TestClient(app)


def _get(client: TestClient, token: str = "viewer"):
    return client.get("/api/keys", headers={"Authorization": f"Bearer {token}"})


def _put(client: TestClient, env_var: str, value: str, token: str = "operator"):
    return client.put(
        f"/api/keys/{env_var}",
        json={"value": value},
        headers={"Authorization": f"Bearer {token}"},
    )


def _delete(client: TestClient, env_var: str, token: str = "operator"):
    return client.delete(f"/api/keys/{env_var}", headers={"Authorization": f"Bearer {token}"})


def test_get_reports_every_packaged_provider(client: TestClient) -> None:
    resp = _get(client)
    assert resp.status_code == 200
    keys = resp.json()["keys"]
    by_var = {entry["env_var"]: entry for entry in keys}
    assert by_var[_DECLARED] == {
        "provider": "anthropic",
        "env_var": _DECLARED,
        "required": True,
        "present": False,
    }


def test_a_stored_key_is_present_but_never_carried(client: TestClient) -> None:
    """``present`` flips; the value is absent from the whole serialized response."""
    assert _put(client, _DECLARED, _SENTINEL).status_code == 200

    resp = _get(client)
    assert resp.status_code == 200
    entry = next(e for e in resp.json()["keys"] if e["env_var"] == _DECLARED)
    assert entry["present"] is True
    assert _SENTINEL not in resp.text
    assert "sk-zzz" not in resp.text


def test_put_never_echoes_the_value(client: TestClient) -> None:
    resp = _put(client, _DECLARED, _SENTINEL)
    assert resp.status_code == 200
    assert resp.json() == {"env_var": _DECLARED, "present": True}
    assert _SENTINEL not in resp.text


def test_put_refuses_an_env_var_no_provider_declares(client: TestClient) -> None:
    resp = _put(client, "LD_PRELOAD", "/tmp/evil.so")
    assert resp.status_code == 400
    assert "LD_PRELOAD" in resp.json()["error"]


def test_put_refuses_an_empty_value(client: TestClient) -> None:
    assert _put(client, _DECLARED, "").status_code == 400


def test_put_refuses_a_value_with_a_line_break_without_echoing_it(
    client: TestClient,
) -> None:
    resp = _put(client, _DECLARED, f"{_SENTINEL}\nOPENAI_API_KEY=forged")
    assert resp.status_code == 400
    assert _SENTINEL not in resp.text
    assert "forged" not in resp.text


def test_put_refuses_a_non_object_body(client: TestClient) -> None:
    resp = client.put(
        f"/api/keys/{_DECLARED}",
        json=[1, 2, 3],
        headers={"Authorization": "Bearer operator"},
    )
    assert resp.status_code == 400


def test_put_refuses_an_oversized_body(client: TestClient) -> None:
    resp = client.put(
        f"/api/keys/{_DECLARED}",
        content=b'{"value": "' + b"x" * 70_000 + b'"}',
        headers={"Authorization": "Bearer operator", "Content-Type": "application/json"},
    )
    assert resp.status_code == 413


def test_delete_forgets_a_stored_key(client: TestClient) -> None:
    _put(client, _DECLARED, _SENTINEL)
    resp = _delete(client, _DECLARED)
    assert resp.status_code == 200
    assert resp.json() == {"env_var": _DECLARED, "present": False, "removed": True}
    entry = next(e for e in _get(client).json()["keys"] if e["env_var"] == _DECLARED)
    assert entry["present"] is False


def test_deleting_a_key_that_was_never_set_is_not_an_error(client: TestClient) -> None:
    resp = _delete(client, "OPENAI_API_KEY")
    assert resp.status_code == 200
    assert resp.json()["removed"] is False


def test_delete_refuses_an_undeclared_env_var(client: TestClient) -> None:
    assert _delete(client, "PATH").status_code == 400


def test_a_viewer_may_read_but_never_write(client: TestClient) -> None:
    assert _get(client, token="viewer").status_code == 200
    assert _put(client, _DECLARED, _SENTINEL, token="viewer").status_code == 403
    assert _delete(client, _DECLARED, token="viewer").status_code == 403


def test_a_viewer_refused_a_write_stores_nothing(client: TestClient, tmp_path: Path) -> None:
    _put(client, _DECLARED, _SENTINEL, token="viewer")
    assert not default_env_file(tmp_path / "arc").exists()
