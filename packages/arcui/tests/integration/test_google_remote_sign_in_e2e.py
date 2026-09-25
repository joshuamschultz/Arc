"""Connecting and RE-connecting Google accounts from the browser, end to end.

Drives the real connector routes, the real :mod:`arcagent.connections` seam, the
real manifest-declared runner and the SHIPPED ``google_workspace`` manifest
against a fake ``gog`` executable (``extensions/tests/fixtures/fake_gog.py``) that
mimics gog v0.34.1's remote two-step flow, its token buckets, and its sign-in
failure shapes. Nothing inside Arc is faked; only the binary at the edge is.

What an operator does, in order: add a connection naming the account, click
"Open Google sign-in" (begin → consent link), sign in with Google, paste the
address the browser landed on (complete → verified status). The same flow is
the Reconnect for an account whose token Google revoked.

And what an attacker does: paste an address that points elsewhere, put flag
text into the account or the address, complete without beginning, complete
somebody else's sign-in, start a second sign-in over a waiting one, and hang
the binary. Each must be refused — most before any process runs — and audited,
and the single-use code must appear nowhere Arc writes.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from arcagent.extension.secrets import LocalFileSecretBackend, SecretRef
from arcagent.modules.connectors.install import connector_env_file
from arcgateway import team_roster
from arctrust.identity import AgentIdentity
from arctrust.paths import arc_team, extensions_dir
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.connectors import routes as connector_routes

_REPO = Path(__file__).resolve().parents[4]
_BUNDLE = _REPO / "extensions" / "google_workspace"
_FAKE_GOG = _REPO / "extensions" / "tests" / "fixtures" / "fake_gog.py"
_AGENT = "google_agent"
_INDUSTRIAL = "josh@blackarcindustrial.com"
_SYSTEMS = "josh@blackarcsystems.com"


# --- the world -----------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An Arc world in ``tmp_path`` with the fake gog first on PATH."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FAKE_GOG_HOME", str(tmp_path / "gog"))
    monkeypatch.delenv("ARC_EXTENSIONS_ROOT", raising=False)
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)
    monkeypatch.delenv("GOG_CLIENT", raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gog = bin_dir / "gog"
    gog.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{_FAKE_GOG}" "$@"\n', encoding="utf-8")
    gog.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    shutil.copytree(
        _BUNDLE,
        extensions_dir(tmp_path / "arc") / "google_workspace",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return tmp_path


def _client(world: Path, *, step_timeout: float | None = None) -> TestClient:
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    key_dir = world / "keys"
    identity.save_keys(key_dir)
    team_root = arc_team(base=world / "arc")
    agent_dir = team_root / _AGENT
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(
        '[agent]\nname = "g"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n'
        '[security]\ntier = "personal"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n',
        encoding="utf-8",
    )
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=connector_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    if step_timeout is not None:
        app.state.connector_step_timeout = step_timeout
    return TestClient(app)


def _headers(token: str = "operator") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _add(
    client: TestClient,
    instance: str,
    account: str,
    *,
    oauth_client: str = "arc",
    read_only: str = "",
) -> Any:
    extra = {"client": oauth_client} if oauth_client else {}
    if read_only:
        extra["read_only"] = read_only
    return client.post(
        "/api/connections",
        json={
            "extension": "google_workspace",
            "instance": instance,
            "agents": [_AGENT],
            "secrets": {"account": account, **extra},
        },
        headers=_headers(),
    )


def _begin(
    client: TestClient, instance: str, token: str = "operator", *, accept: bool = False
) -> Any:
    body = {"accept_warnings": True} if accept else {}
    return client.post(
        f"/api/connections/{instance}/sign-in/begin", json=body, headers=_headers(token)
    )


def _complete(client: TestClient, instance: str, url: str, token: str = "operator") -> Any:
    return client.post(
        f"/api/connections/{instance}/sign-in/complete",
        json={"redirect_url": url},
        headers=_headers(token),
    )


def _landed(consent_url: str, signed_in_as: str) -> str:
    """The address the browser lands on after "Google" signs ``signed_in_as`` in."""
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(consent_url).query)
    redirect = query["redirect_uri"][0]
    code = quote(f"code-for:{signed_in_as}", safe="")
    return f"{redirect}?state={query['state'][0]}&code={code}&scope=email&authuser=0"


def _status(client: TestClient, instance: str) -> dict[str, Any]:
    resp = client.get(f"/api/connections/{instance}/auth-status", headers=_headers("viewer"))
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _calls(world: Path) -> list[dict[str, Any]]:
    path = world / "gog" / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _sign_in_calls(world: Path) -> list[dict[str, Any]]:
    return [call for call in _calls(world) if call["argv"][:2] == ["auth", "add"]]


def _mark(world: Path, account: str, state: str, client: str = "arc") -> None:
    path = world / "gog" / "tokens.json"
    tokens = json.loads(path.read_text()) if path.exists() else {}
    tokens[f"{client}:{account}"] = state
    path.write_text(json.dumps(tokens))


def _files_holding(root: Path, needle: str) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and needle.encode() in path.read_bytes()
    ]


# --- the path an operator walks -------------------------------------------------


def test_two_accounts_sign_in_independently_and_each_reports_working(world: Path) -> None:
    client = _client(world)
    assert _add(client, "blackarc", _INDUSTRIAL).status_code == 200
    assert _add(client, "systems", _SYSTEMS).status_code == 200
    assert _status(client, "blackarc")["sign_in"] == "signed_out"
    assert _status(client, "systems")["sign_in"] == "signed_out"

    for instance, account in (("blackarc", _INDUSTRIAL), ("systems", _SYSTEMS)):
        begun = _begin(client, instance)
        assert begun.status_code == 200, begun.text
        body = begun.json()
        assert body["account"] == account
        assert body["consent_url"].startswith("https://accounts.google.com/")
        assert body["expires_in"] > 0
        done = _complete(client, instance, _landed(body["consent_url"], account))
        assert done.status_code == 200, done.text
        assert done.json()["sign_in"] == "signed_in"

    # Each check ran as its own account — the two never crossed.
    assert _status(client, "blackarc")["sign_in"] == "signed_in"
    assert _status(client, "systems")["sign_in"] == "signed_in"
    checks = [call for call in _calls(world) if call["argv"][:3] == ["gmail", "labels", "get"]]
    assert {call["account"] for call in checks} == {_INDUSTRIAL, _SYSTEMS}
    adds = _sign_in_calls(world)
    assert [call["argv"][2] for call in adds] == [_INDUSTRIAL] * 2 + [_SYSTEMS] * 2
    assert all(call["account"] == call["argv"][2] for call in adds)


def test_a_revoked_token_reads_reconnect_needed_and_reconnecting_fixes_it(world: Path) -> None:
    client = _client(world)
    assert _add(client, "blackarc", _INDUSTRIAL).status_code == 200
    _mark(world, _INDUSTRIAL, "expired")

    status = _status(client, "blackarc")
    assert status["sign_in"] == "expired"
    assert "invalid_grant" in status["detail"]

    begun = _begin(client, "blackarc").json()
    done = _complete(client, "blackarc", _landed(begun["consent_url"], _INDUSTRIAL))
    assert done.status_code == 200, done.text
    assert done.json()["sign_in"] == "signed_in"


def test_a_named_oauth_client_is_used_for_sign_in_and_for_the_check(world: Path) -> None:
    """The durable fix: the operator's own published client, end to end."""
    client = _client(world)
    assert (
        _add(client, "hello", "hello@joshuaschultz.com", oauth_client="Arc-Prod").status_code
        == 200
    )

    begun = _begin(client, "hello").json()
    done = _complete(client, "hello", _landed(begun["consent_url"], "hello@joshuaschultz.com"))
    assert done.json()["sign_in"] == "signed_in", done.text
    tokens = json.loads((world / "gog" / "tokens.json").read_text())
    assert tokens == {"arc-prod:hello@joshuaschultz.com": "good"}
    adds = _sign_in_calls(world)
    assert all("--client=arc-prod" in call["argv"] for call in adds)
    assert {call["client"] for call in _calls(world) if call["argv"] != ["--version"]} == {
        "arc-prod"
    }


def test_signing_in_as_the_wrong_google_account_is_refused(world: Path) -> None:
    client = _client(world)
    assert _add(client, "blackarc", _INDUSTRIAL).status_code == 200
    begun = _begin(client, "blackarc").json()

    done = _complete(client, "blackarc", _landed(begun["consent_url"], _SYSTEMS))

    assert done.status_code == 400
    assert f"expected {_INDUSTRIAL}" in done.json()["error"]
    assert _status(client, "blackarc")["sign_in"] == "signed_out"


# --- what an attacker tries -------------------------------------------------------


def test_the_sign_in_routes_are_operator_only(world: Path) -> None:
    client = _client(world)
    assert _add(client, "blackarc", _INDUSTRIAL).status_code == 200
    assert _begin(client, "blackarc", token="viewer").status_code == 403
    assert (
        _complete(client, "blackarc", "http://127.0.0.1:1/oauth2/callback", "viewer").status_code
        == 403
    )
    assert _sign_in_calls(world) == []


@pytest.mark.parametrize(
    "pasted",
    [
        "https://evil.example/oauth2/callback?state=x&code=y",
        "http://evil.example:40000/oauth2/callback?state=x&code=y",
        "http://127.0.0.1:40000/other?state=x&code=y",
        "http://127.0.0.1:40000/oauth2/callback?state=x&code=y&extra=1",
        "http://127.0.0.1:40000/oauth2/callback?state=x&code=y --force-consent",
        "--auth-url=http://127.0.0.1:40000/oauth2/callback?state=x&code=y",
        "http://127.0.0.1:40000/oauth2/callback?state=x&code=y;touch${IFS}/tmp/p",
        "http://127.0.0.1:40000/oauth2/callback?" + "a" * 4000,
    ],
)
def test_a_bad_address_is_refused_before_any_process_runs(world: Path, pasted: str) -> None:
    client = _client(world)
    assert _add(client, "blackarc", _INDUSTRIAL).status_code == 200
    assert _begin(client, "blackarc").status_code == 200
    before = len(_sign_in_calls(world))

    done = _complete(client, "blackarc", pasted)

    assert done.status_code == 400
    assert len(_sign_in_calls(world)) == before
    # A typo does not cost the operator their begun sign-in.
    begun = _begin(client, "blackarc").json()
    assert (
        _complete(client, "blackarc", _landed(begun["consent_url"], _INDUSTRIAL)).status_code
        == 200
    )


@pytest.mark.parametrize(
    "account",
    ["--help@x.com", "a b@x.com", "x@y.com --services all", "$(id)@x.com", "-x"],
)
def test_flag_text_in_the_account_never_reaches_argv(world: Path, account: str) -> None:
    """Stored before this check existed, or hand-edited: refused at sign-in, not run."""
    client = _client(world)
    assert _add(client, "blackarc", _INDUSTRIAL).status_code == 200
    backend = LocalFileSecretBackend(connector_env_file(world / "arc"))
    asyncio.run(backend.put(SecretRef(connection="blackarc", field="account"), account))

    resp = _begin(client, "blackarc")

    assert resp.status_code == 400
    assert "not a plain email address" in resp.json()["error"]
    assert account not in resp.json()["error"]
    assert _sign_in_calls(world) == []


def test_complete_without_begin_is_refused_unrun(world: Path) -> None:
    client = _client(world)
    assert _add(client, "blackarc", _INDUSTRIAL).status_code == 200
    resp = _complete(
        client, "blackarc", "http://127.0.0.1:40000/oauth2/callback?state=abc&code=code-for:x"
    )
    assert resp.status_code == 400
    assert "no sign-in is waiting" in resp.json()["error"]
    assert _sign_in_calls(world) == []


def test_completing_another_connections_sign_in_is_refused(world: Path) -> None:
    client = _client(world)
    assert _add(client, "blackarc", _INDUSTRIAL).status_code == 200
    assert _add(client, "systems", _SYSTEMS).status_code == 200
    begun = _begin(client, "blackarc").json()

    resp = _complete(client, "systems", _landed(begun["consent_url"], _SYSTEMS))

    assert resp.status_code == 400
    assert len(_sign_in_calls(world)) == 1


def test_a_second_begin_does_not_clobber_a_waiting_one(world: Path) -> None:
    client = _client(world)
    assert _add(client, "blackarc", _INDUSTRIAL).status_code == 200
    assert _add(client, "systems", _SYSTEMS).status_code == 200
    first = _begin(client, "blackarc").json()

    second = _begin(client, "systems")

    assert second.status_code == 400
    assert _INDUSTRIAL in second.json()["error"]
    # The waiting one still completes.
    done = _complete(client, "blackarc", _landed(first["consent_url"], _INDUSTRIAL))
    assert done.json()["sign_in"] == "signed_in"
    # And once it has, the next account may start.
    assert _begin(client, "systems").status_code == 200


def test_a_hung_binary_is_killed_and_reported(world: Path) -> None:
    client = _client(world, step_timeout=1.0)
    assert _add(client, "blackarc", _INDUSTRIAL).status_code == 200
    (world / "gog" / "hang").write_text("")

    resp = _begin(client, "blackarc")

    assert resp.status_code == 400
    assert "did not finish" in resp.json()["error"]


def test_the_code_is_never_written_anywhere_arc_writes(world: Path) -> None:
    client = _client(world)
    assert _add(client, "blackarc", _INDUSTRIAL).status_code == 200
    begun = _begin(client, "blackarc").json()
    landed = _landed(begun["consent_url"], _INDUSTRIAL)

    done = _complete(client, "blackarc", landed)

    assert done.status_code == 200
    code = quote(f"code-for:{_INDUSTRIAL}", safe="")
    assert code not in done.text
    assert _INDUSTRIAL.replace("@", "%40") not in done.text
    arc_owned = [world / "arc", world / "data"]
    assert [hit for root in arc_owned for hit in _files_holding(root, code)] == []
    assert [hit for root in arc_owned for hit in _files_holding(root, landed)] == []


# --- scopes, and the legacy client -------------------------------------------------


def test_sign_in_asks_for_read_only_scopes_unless_drafting_is_chosen(world: Path) -> None:
    client = _client(world)
    assert _add(client, "blackarc", _INDUSTRIAL).status_code == 200
    assert _add(client, "systems", _SYSTEMS, read_only="no").status_code == 200

    for instance, account in (("blackarc", _INDUSTRIAL), ("systems", _SYSTEMS)):
        begun = _begin(client, instance).json()
        landed = _landed(begun["consent_url"], account)
        assert _complete(client, instance, landed).status_code == 200

    by_account = {call["argv"][2]: call["argv"] for call in _sign_in_calls(world)}
    assert "--readonly=yes" in by_account[_INDUSTRIAL]
    assert "--readonly=no" in by_account[_SYSTEMS]
    for argv in by_account.values():
        assert argv[argv.index("--services") + 1] == "gmail,calendar,drive"
        assert argv[argv.index("--drive-scope") + 1] == "readonly"


def test_a_choice_outside_its_set_is_refused_at_entry(world: Path) -> None:
    client = _client(world)
    assert _add(client, "blackarc", _INDUSTRIAL, read_only="maybe").status_code == 400


def test_the_built_in_client_is_a_labelled_fallback_that_must_be_accepted(world: Path) -> None:
    client = _client(world)
    assert _add(client, "legacy", _INDUSTRIAL, oauth_client="").status_code == 200

    auth = client.get("/api/connections/legacy/auth", headers=_headers("viewer")).json()
    warning = next(row["warning"] for row in auth["credentials"] if row["name"] == "client")
    assert "about 7 days" in warning

    refused = _begin(client, "legacy")
    assert refused.status_code == 400
    assert "about 7 days" in refused.json()["error"]
    assert _sign_in_calls(world) == []

    begun = _begin(client, "legacy", accept=True)
    assert begun.status_code == 200, begun.text
    done = _complete(client, "legacy", _landed(begun.json()["consent_url"], _INDUSTRIAL))
    assert done.json()["sign_in"] == "signed_in"
    assert all("--client=" in call["argv"] for call in _sign_in_calls(world))
    tokens = json.loads((world / "gog" / "tokens.json").read_text())
    assert tokens == {f"default:{_INDUSTRIAL}": "good"}
