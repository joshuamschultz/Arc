"""A person pastes a credential into the browser and a host binary is signed in.

This is the path the credential seam exists for, driven end to end: the operator
POSTs a value to the connector routes, ``[secrets.placement]`` puts it in the
environment of the program the bundle declared, and ``auth-status`` answers from
that program rather than from a probe.

The bundle here is a ``cli`` one — the shape the web could not finish before, and
still the shape that matters, because ``build_attachment`` refused any ``cli``
bundle that declared a credential at all. Its "binary" is this interpreter, and
both the probe and the sign-in check fail unless the placed variable reached the
child. So a route that stored the credential and delivered it nowhere fails here;
it cannot pass by writing a file.

Nothing about the web is vendor-aware and these tests are what keeps it that way:
:mod:`arcui.routes.connectors` is unchanged by the seam. It collects a ``secrets``
mapping of names to values exactly as it did for a ``native`` bundle, hands it to
:mod:`arcagent.connections`, and never learns that a placement exists.

**The leak assertion is a filesystem sweep, not a response check.** A sentinel is
posted, and afterwards every file in the whole temporary Arc world is read: the
audit chain, the connection state, the agent config, and anything a log wrote.
The credential may appear in exactly one of them — the agent's own owner-only
secret store — and a single other hit fails the test. Checking only the response
body would have missed the audit event, and checking only the audit event would
have missed the chain.
"""

from __future__ import annotations

import json
import logging
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
from arcagent.modules.connectors.install import connector_env_file
from arcgateway import team_roster
from arctrust.identity import AgentIdentity
from arctrust.paths import arc_team, extensions_dir
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.connectors import routes as connector_routes

#: Distinctive enough that finding it anywhere is proof of a leak rather than a
#: coincidence of phrasing.
_SENTINEL = "zzz-placed-credential-sentinel-8823"

_EXTENSION = "acme_placed"
_INSTANCE = "work"

#: The variable this bundle's tool reads its credential from. Arc knows nothing
#: about it beyond what the manifest below says.
_VARIABLE = "ACME_PLACED_TOKEN"


def _gate(exit_expression: str) -> str:
    """A command that succeeds only when the placed variable arrived."""
    script = f"import os,sys; sys.exit({exit_expression})"
    return f"{sys.executable} -c {shlex.quote(script)}"


#: Exits 0 and prints JSON only with the credential present — so the install's own
#: probe is the delivery proof, and no separate "did it arrive" assertion is needed.
_PROBE_SCRIPT = (
    f"import os,sys; sys.stdout.write('{{}}') if os.environ.get({_VARIABLE!r}) else sys.exit(1)"
)

#: Stands in for ``dbxcli account``: answers whether THIS account is connected,
#: which is a different question from whether the binary runs.
_VERIFY = _gate(f"0 if os.environ.get({_VARIABLE!r}) else 2")


def _manifest(*, placed: bool = True) -> str:
    placement = f"\n[secrets.placement]\nvariable = {json.dumps(_VARIABLE)}\n" if placed else "\n"
    return f"""
[extension]
name = "{_EXTENSION}"
version = "1.0.0"
attachment = "cli"
description = "Acme through a binary that reads its token from the environment."

[[host_requires]]
name = {json.dumps(sys.executable)}
authorize_command = "acme login"
verify_command = {json.dumps(_VERIFY)}
instruction = "Install the acme CLI on this host, then paste a token into Arc."

[[secrets]]
name = "access_token"
prompt = "Acme access token, from the Acme console under Settings then API tokens."
{placement}
[tools]
allow = ["acme_ping"]

[[tools.declared]]
name = "acme_ping"
description = "Report which account this connection is authorised as."
classification = "read_only"

[approval]
default = "outbound"

[config.cli]
binary = {json.dumps(sys.executable)}
probe_argv = ["-c", {json.dumps(_PROBE_SCRIPT)}]
install_instruction = "See [[host_requires]]."

[[config.cli.commands]]
tool = "acme_ping"
argv = ["-c", {json.dumps(_PROBE_SCRIPT)}]
description = "Report which account this connection is authorised as."
classification = "read_only"
"""


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the whole Arc world into ``tmp_path`` so the sweep below is total."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ARC_EXTENSIONS_ROOT", raising=False)
    return tmp_path


#: The agent directory name — the coordinate a grant is written against.
_AGENT = "acme_agent"


def _arc_dir(world: Path) -> Path:
    """The deployment root: connections, credentials and the bundle search path."""
    return world / "arc"


def _env_file(world: Path) -> Path:
    """The one owner-only file a connector credential may be written to."""
    return connector_env_file(_arc_dir(world))


def _agent(world: Path) -> tuple[TestClient, str, Path]:
    """A one-agent fleet and an app carrying only the connector routes."""
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    key_dir = world / "keys"
    identity.save_keys(key_dir)
    team_root = arc_team(base=_arc_dir(world))
    agent_dir = team_root / _AGENT
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(
        '[agent]\nname = "acme"\norg = "arc"\ntype = "exec"\n'
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
    return TestClient(app), "acme", agent_dir


def _write_bundle(world: Path, *, placed: bool = True) -> Path:
    """Put the bundle on the DEPLOYMENT's search path; there is no agent-local one."""
    bundle = extensions_dir(_arc_dir(world)) / _EXTENSION
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "extension.toml").write_text(_manifest(placed=placed), encoding="utf-8")
    return bundle


def _headers(token: str = "operator") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _install(client: TestClient, *, value: str = _SENTINEL) -> Any:
    return client.post(
        "/api/connections",
        json={
            "extension": _EXTENSION,
            "instance": _INSTANCE,
            "agents": [_AGENT],
            "secrets": {"access_token": value},
        },
        headers=_headers(),
    )


def _files_holding(root: Path, needle: str) -> list[Path]:
    """Every file under ``root`` whose bytes contain ``needle``."""
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            blob = path.read_bytes()
        except OSError:  # reason: a socket or a permission-denied path is not a leak
            continue
        if needle.encode() in blob:
            found.append(path)
    return found


def test_host_setup_uses_the_bundle_coordinate_not_its_display_name() -> None:
    """A display label with spaces must not become an extension lookup key."""
    source = (
        Path(__file__).parents[1] / "web" / "src" / "components" / "connector-secrets-sheet.tsx"
    ).read_text(encoding="utf-8")

    assert "extension={bundle.name}" in source


# --- the path a person walks --------------------------------------------------


def test_a_pasted_credential_reaches_the_binary_and_the_connection_installs(
    world: Path,
) -> None:
    """The install's own probe runs the binary and fails without the credential.

    So a 200 here is not "the route accepted a value": it is "the value was
    stored, read back out of the store, put in the environment of a real child
    process, and that process answered". Nothing weaker can produce this result.
    """
    client, _agent_id, _dir = _agent(world)
    _write_bundle(world)

    resp = _install(client)

    assert resp.status_code == 200, resp.text
    assert resp.json()["tools"] == ["acme_ping"]


def test_the_same_bundle_without_a_placement_is_refused_by_name(world: Path) -> None:
    """Fails closed. A credential with nowhere to go must not install quietly.

    This is the original defect stated as a route test: before the seam, a stored
    credential that reached no program was the *only* outcome available to a
    ``cli`` bundle. The refusal names the field so an operator can act on it, and
    carries no part of what they typed.
    """
    client, _agent_id, _dir = _agent(world)
    _write_bundle(world, placed=False)

    resp = _install(client)

    assert resp.status_code == 400
    assert "access_token" in resp.json()["error"]
    assert _SENTINEL not in resp.text


def test_auth_status_reports_signed_in_from_the_placed_credential(world: Path) -> None:
    """The badge answers from ``verify_command`` run WITH the placement.

    Without the placed environment this check exits 2 — so a green badge here is
    the manifest's own sign-in command agreeing, not the probe wearing its name.
    """
    client, _agent_id, _dir = _agent(world)
    _write_bundle(world)
    assert _install(client).status_code == 200

    body = client.get(
        f"/api/connections/{_INSTANCE}/auth-status", headers=_headers("viewer")
    ).json()

    assert body["sign_in"] == "signed_in"


def test_a_connection_whose_credential_was_forgotten_reports_signed_out(world: Path) -> None:
    """Removing the credential must flip the badge, or the badge is decoration.

    The store is emptied behind the connection's back — what a rotation that
    failed, or an operator clearing a vault entry, really looks like. The check
    then runs without the placement and says so, rather than reporting the last
    good answer.
    """
    client, _agent_id, _dir = _agent(world)
    _write_bundle(world)
    assert _install(client).status_code == 200
    _env_file(world).write_text("", encoding="utf-8")
    _env_file(world).chmod(0o600)

    body = client.get(
        f"/api/connections/{_INSTANCE}/auth-status", headers=_headers("viewer")
    ).json()

    assert body["sign_in"] == "signed_out"


# --- the value goes exactly one place ----------------------------------------


def test_the_pasted_value_is_written_only_to_the_agents_own_secret_store(
    world: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Sweep the whole world: one file may hold it, and it is not a log or a chain.

    The audit chain, the connection state, the agent config, and every log record
    are all produced by this one request. Asserting on the response body alone
    would pass while the credential sat in the chain forever.
    """
    client, _agent_id, _dir = _agent(world)
    _write_bundle(world)

    with caplog.at_level(logging.DEBUG):
        resp = _install(client)
        status = client.get(
            f"/api/connections/{_INSTANCE}/auth-status",
            headers=_headers("viewer"),
        )
        doctor = client.get(f"/api/connections/{_INSTANCE}/doctor", headers=_headers("viewer"))

    assert resp.status_code == 200
    assert _SENTINEL not in resp.text
    assert _SENTINEL not in status.text
    assert _SENTINEL not in doctor.text
    assert _SENTINEL not in "".join(record.getMessage() for record in caplog.records)
    assert _files_holding(world, _SENTINEL) == [_env_file(world)]


def test_removing_the_connection_takes_the_credential_back_out(world: Path) -> None:
    """The one file allowed to hold it must not keep holding it afterwards."""
    client, _agent_id, _dir = _agent(world)
    _write_bundle(world)
    assert _install(client).status_code == 200

    removed = client.delete(f"/api/connections/{_INSTANCE}", headers=_headers())

    assert removed.status_code == 200
    assert removed.json()["removed_secrets"] == ["access_token"]
    assert _files_holding(world, _SENTINEL) == []


def test_the_web_never_learns_that_a_placement_exists(world: Path) -> None:
    """ADR-030's governing test, at the surface: the paste field is bundle-agnostic.

    The route offers the operator exactly the fields the manifest declares, with
    the manifest's own prompt, and its answer names fields and never values. A
    placement changes what Arc DOES with the value and nothing about what the web
    asks for — which is why no file under ``arcui`` changed for this seam.
    """
    client, _agent_id, _dir = _agent(world)
    _write_bundle(world)
    assert _install(client).status_code == 200

    body = client.get(f"/api/connections/{_INSTANCE}/auth", headers=_headers("viewer")).json()

    assert body["credentials"] == [
        {
            "name": "access_token",
            "prompt": "Acme access token, from the Acme console under Settings then API tokens.",
            # Declared nothing, so masked — and therefore never read back.
            "sensitive": True,
            "value": "",
        }
    ]
    assert "placement" not in str(body)
    assert _VARIABLE not in str(body)
