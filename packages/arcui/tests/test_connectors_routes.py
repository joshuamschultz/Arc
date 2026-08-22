"""SPEC-064 T-021 — the connector routes: the web makes a real connection.

Every install here goes through the real
:func:`arcagent.modules.connectors.install.install_connector` against a real
bundle on disk. A test that substitutes the install proves only that the route
can call a substitute — and the ordering it would stop checking (signature gate
before attachment, credential rolled back on a failed probe) is the security
property the whole module exists for.

Three refusals get their own tests because each one is a place a defect would be
invisible: a credential in a response body, a host prerequisite silently
installed, and a write that lands before a refusal.
"""

from __future__ import annotations

import hashlib
import io
import json
import shlex
import sys
import tarfile
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arcagent.connections import HostPrerequisiteDirector
from arcagent.core.config import ToolConfig, ToolsConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.tool_registry import ToolRegistry
from arcagent.extension.grants import ConnectionRegistry
from arcagent.extension.platforms import host_platform
from arcagent.modules.connectors import _runtime
from arcagent.modules.connectors.capabilities import Connectors
from arcagent.modules.connectors.install import connector_env_file
from arcagent.tools.human_gate import HumanGate
from arcgateway import team_roster
from arcstore.backends.memory import FakeBackend
from arctrust.identity import AgentIdentity
from arctrust.paths import arc_team, extensions_dir
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.connectors import routes as connector_routes

#: Distinctive enough that finding it anywhere in a response is proof of a leak.
_SENTINEL = "zzz-web-connector-sentinel-4711"

_EXTENSION = "acme_tickets"
_INSTANCE = "work"

_MANIFEST = """
[extension]
name = "acme_tickets"
version = "2.1.0"
attachment = "native"
description = "Open and read Acme tickets."

[config.native]
entrypoint = "acme_attachment"

[[secrets]]
name = "api_token"
prompt = "Paste the Acme API token"

[tools]
allow = ["ping"]

[[tools.declared]]
name = "ping"
description = "Report the Acme client version."
classification = "read_only"

[approval]
default = "outbound"
"""

#: The bundle's own implementation, written beside its manifest as a real bundle
#: does. A ``native`` bundle rather than a ``cli`` one because this suite's whole
#: subject is a CREDENTIAL travelling from the web form to the connector, and a
#: ``cli`` attachment reaches its service by spawning a binary — it has no way to
#: receive one, which ``build_attachment`` refuses by name.
#:
#: So the adapter probes reachable only when it was handed the credential. That
#: makes ``test_probe_reports_a_live_connection`` an assertion that the route
#: DELIVERED it, not merely that it stored it somewhere.
_ADAPTER = '''
"""The acme fixture's own implementation, outside every Arc package."""

from __future__ import annotations

from typing import Any

from arcagent.extension.attachment import ProbeResult, ToolResult, ToolSpec


class AcmeAttachment:
    """Reachable exactly when Arc handed it the credential the manifest declares."""

    def __init__(self, context: dict[str, Any]) -> None:
        self._token = str(context.get("api_token") or "")

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        if not self._token:
            return ProbeResult(reachable=False, detail="acme has no credential for api_token")
        return ProbeResult(
            reachable=True, tools=await self.describe_tools(), detail="acme is authenticated"
        )

    async def describe_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="ping",
                description="Report the Acme client version.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                classification="read_only",
            )
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, content="acme 2.1.0")


def build_native_attachment(context: dict[str, Any]) -> AcmeAttachment:
    return AcmeAttachment(context)
'''

#: The command the hosted fixture's binary is authorised with — the string a
#: browser must be able to show an operator who has no credential to type.
_AUTHORIZE_COMMAND = "acme auth login --scopes read"

#: A bundle in the shape ``github``, ``dropbox``, ``google_workspace`` and
#: ``readwise_reader`` really have: no ``[[secrets]]`` at all, because the binary
#: holds its own token. Its own entrypoint module so the always-reachable
#: implementation cannot be confused with the credential-checking one above.
_MANIFEST_HOSTED = f"""
[extension]
name = "{_EXTENSION}"
version = "1.0.0"
attachment = "native"
description = "Acme through a binary that keeps its own token."

[config.native]
entrypoint = "acme_hosted_attachment"

[[host_requires]]
name = "sh"
authorize_command = "{_AUTHORIZE_COMMAND}"
instruction = "Install the acme CLI, then authorise it."

[tools]
allow = ["ping"]

[[tools.declared]]
name = "ping"
description = "Report the Acme client version."
classification = "read_only"

[approval]
default = "outbound"
"""

_ADAPTER_HOSTED = '''
"""A connector whose binary owns its authentication: Arc holds no credential."""

from __future__ import annotations

from typing import Any

from arcagent.extension.attachment import ProbeResult, ToolResult, ToolSpec


class HostedAttachment:
    """Always reachable — the host binary, not Arc, decides whether it is authorised."""

    def __init__(self, context: dict[str, Any]) -> None:
        self._context = context

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        return ProbeResult(
            reachable=True, tools=await self.describe_tools(), detail="acme 2.1.0"
        )

    async def describe_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="ping",
                description="Report the Acme client version.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                classification="read_only",
            )
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, content="acme 2.1.0")


def build_native_attachment(context: dict[str, Any]) -> HostedAttachment:
    return HostedAttachment(context)
'''

#: A connector whose reachability tracks a real sign-in rather than always
#: answering yes. ``authorize`` is only meaningful if the probe behind it can
#: change, so this attachment is reachable exactly when its host binary has left
#: a marker beside the bundle — which is what the login command below writes.
#: Without it, "authorize reported success" would be true of doing nothing.
_ADAPTER_SIGNIN = '''
"""A connector that is reachable only once its host binary has signed in."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcagent.extension.attachment import ProbeResult, ToolResult, ToolSpec

SIGNED_IN_MARKER = ".acme-signed-in"


class SignInAttachment:
    def __init__(self, context: dict[str, Any]) -> None:
        self._marker = Path(str(context.get("bundle"))) / SIGNED_IN_MARKER

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        if not self._marker.exists():
            return ProbeResult(reachable=False, detail="acme is not signed in")
        return ProbeResult(
            reachable=True, tools=await self.describe_tools(), detail="acme is signed in"
        )

    async def describe_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="ping",
                description="Report the Acme client version.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                classification="read_only",
            )
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, content="acme 2.1.0")


def build_native_attachment(context: dict[str, Any]) -> SignInAttachment:
    return SignInAttachment(context)
'''

#: The marker filename the sign-in adapter watches, kept in one place so the
#: manifest's login command and the adapter cannot drift apart.
_SIGNED_IN_MARKER = ".acme-signed-in"


def _signin_manifest(
    *,
    host: str,
    authorize: str,
    token_login: str = "",
    verify: str = "",
    entrypoint: str = "acme_signin_attachment",
) -> str:
    """A no-secrets bundle whose reachability follows a real sign-in.

    ``token_login`` empty is the interactive-only shape — ``dbxcli login``,
    ``gog auth add`` — where the honest answer is the command and nothing else.
    ``verify`` empty is the bundle that declares no way to prove a sign-in, whose
    only honest answer is "not known".
    """
    token_clause = f"token_command = {json.dumps(token_login)}\n" if token_login else ""
    verify_clause = f"verify_command = {json.dumps(verify)}\n" if verify else ""
    return f"""
[extension]
name = "{_EXTENSION}"
version = "1.0.0"
attachment = "native"
description = "Acme through a binary that keeps its own token."

[config.native]
entrypoint = "{entrypoint}"

[[host_requires]]
name = {json.dumps(host)}
authorize_command = {json.dumps(authorize)}
{token_clause}{verify_clause}instruction = "Install the acme CLI on this host, then authorise it."

[tools]
allow = ["ping"]

[[tools.declared]]
name = "ping"
description = "Report the Acme client version."
classification = "read_only"

[approval]
default = "outbound"
"""


def _token_login_command(bundle: Path) -> str:
    """A real non-interactive login: reads the token on stdin, then signs in.

    This interpreter stands in for ``gh auth login --with-token``. It is a real
    subprocess reading real stdin, so a route that put the token on argv, or
    never delivered it at all, fails here rather than passing against a stub.
    """
    script = (
        "import sys, pathlib;"
        " token = sys.stdin.read().strip();"
        f" pathlib.Path({str(bundle / _SIGNED_IN_MARKER)!r}).write_text('ok') if token else None;"
        " sys.exit(0 if token else 1)"
    )
    return f"{sys.executable} -c {shlex.quote(script)}"


def _verify_command(bundle: Path) -> str:
    """The real second question: is this binary signed in *right now*?

    Stands in for ``dbxcli account`` / ``gh auth status``. It reads the same
    marker the login writes, so a route that reported the PROBE instead of this
    — which is the shipped defect — fails here rather than passing against a
    binary that merely starts.
    """
    script = (
        "import pathlib, sys;"
        f" sys.exit(0 if pathlib.Path({str(bundle / _SIGNED_IN_MARKER)!r}).exists() else 1)"
    )
    return f"{sys.executable} -c {shlex.quote(script)}"


_MANIFEST_NEEDS_HOST = (
    _MANIFEST
    + """
[[host_requires]]
name = "definitely_not_installed_xyz"
instruction = "brew install definitely-not-installed-xyz"
"""
)


#: The agent directory name — the coordinate a grant is matched against when the
#: agent starts. Deliberately not the same string as the roster's ``agent_id``
#: ("acme"), so a route that granted the id instead of the name writes a grant
#: that is listed and effective for nobody, and fails here.
_AGENT = "acme_agent"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the whole Arc world into ``tmp_path``.

    ``ARC_EXTENSIONS_ROOT`` is cleared so a value in the developer's environment
    cannot add a bundle the assertions do not expect.
    """
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ARC_EXTENSIONS_ROOT", raising=False)
    return tmp_path


def _arc_dir(world: Path) -> Path:
    """The deployment root: connections, credentials, bundles and the fleet config."""
    return world / "arc"


def _bundles(world: Path) -> Path:
    """The deployment's bundle root. There is deliberately no agent-local one."""
    return extensions_dir(_arc_dir(world))


def _env_file(world: Path) -> Path:
    """The one owner-only file every connector credential is written to."""
    return connector_env_file(_arc_dir(world))


def _agent(world: Path) -> tuple[TestClient, str, Path]:
    """A one-agent fleet, an app carrying only the connector routes, two tokens.

    The agent lives under ``<arc_dir>/team`` because that is where the grant model
    reads an agent's tier from — the stringency a connection granted to it must
    be served at.
    """
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
    app.state.arcstore_backend = FakeBackend()
    return TestClient(app), "acme", agent_dir


def _write_bundle(root: Path, name: str = _EXTENSION, manifest: str = _MANIFEST) -> Path:
    bundle = root / name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "extension.toml").write_text(manifest, encoding="utf-8")
    (bundle / "acme_attachment.py").write_text(_ADAPTER, encoding="utf-8")
    (bundle / "acme_hosted_attachment.py").write_text(_ADAPTER_HOSTED, encoding="utf-8")
    (bundle / "acme_signin_attachment.py").write_text(_ADAPTER_SIGNIN, encoding="utf-8")
    return bundle


def _headers(token: str = "operator") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _install(
    client: TestClient,
    *,
    instance: str = _INSTANCE,
    secrets: dict[str, str] | None = None,
    agents: list[str] | None = None,
    token: str = "operator",
) -> Any:
    return client.post(
        "/api/connections",
        json={
            "extension": _EXTENSION,
            "instance": instance,
            "agents": [_AGENT] if agents is None else agents,
            "secrets": {"api_token": _SENTINEL} if secrets is None else secrets,
        },
        headers=_headers(token),
    )


def _defined(world: Path) -> dict[str, Any]:
    """The deployment's connections, read off disk exactly as the runtime reads them."""
    path = ConnectionRegistry(_arc_dir(world)).path
    if not path.is_file():
        return {}
    table = tomllib.loads(path.read_text(encoding="utf-8")).get("connections", {})
    assert isinstance(table, dict)
    return table


# --- catalog ---------------------------------------------------------------


def test_catalog_lists_a_readable_bundle(world: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fleet = world / "fleet_extensions"
    _write_bundle(fleet)
    monkeypatch.setenv("ARC_EXTENSIONS_ROOT", str(fleet))
    client, _agent_id, _dir = _agent(world)

    resp = client.get("/api/connectors/catalog", headers=_headers("viewer"))
    assert resp.status_code == 200
    body = resp.json()
    entry = next(e for e in body["available"] if e["name"] == _EXTENSION)
    assert entry["version"] == "2.1.0"
    assert entry["description"] == "Open and read Acme tickets."
    assert entry["attachment"] == "native"
    assert entry["approval_default"] == "outbound"
    assert entry["secrets"] == [
        # ``sensitive`` defaults true and the catalog carries no value: nothing is
        # connected yet, so there is nothing configured to read back.
        {"name": "api_token", "prompt": "Paste the Acme API token", "sensitive": True, "value": ""}
    ]
    assert entry["root"] == str(fleet)
    assert body["unreadable"] == []


def test_catalog_reports_an_unparseable_bundle_instead_of_500ing(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = world / "fleet_extensions"
    _write_bundle(fleet)
    _write_bundle(fleet, name="brokenbundle", manifest="this is not = valid toml [[[")
    monkeypatch.setenv("ARC_EXTENSIONS_ROOT", str(fleet))
    client, _agent_id, _dir = _agent(world)

    resp = client.get("/api/connectors/catalog", headers=_headers("viewer"))
    assert resp.status_code == 200
    body = resp.json()
    assert [e["name"] for e in body["available"]] == [_EXTENSION]
    broken = next(e for e in body["unreadable"] if e["name"] == "brokenbundle")
    assert broken["reason"]


# --- listings ---------------------------------------------------------------


def test_the_deployment_listing_is_empty_before_anything_is_connected(world: Path) -> None:
    _write_bundle(_bundles(world))
    client, _agent_id, _dir = _agent(world)
    resp = client.get("/api/connections", headers=_headers("viewer"))
    assert resp.status_code == 200
    assert resp.json() == {
        "connections": [],
        "extensions_roots": [str(_bundles(world))],
    }


def test_agent_connectors_is_empty_before_anything_is_granted(world: Path) -> None:
    client, agent_id, _dir = _agent(world)
    resp = client.get(f"/api/agents/{agent_id}/connectors", headers=_headers("viewer"))
    assert resp.status_code == 200
    assert resp.json() == {"instances": [], "extensions_roots": []}


def test_agent_connectors_is_404_for_an_unknown_agent(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    resp = client.get("/api/agents/nobody/connectors", headers=_headers("viewer"))
    assert resp.status_code == 404


# --- install ---------------------------------------------------------------


def test_install_goes_through_the_real_path_and_persists(world: Path) -> None:
    client, agent_id, agent_dir = _agent(world)
    _write_bundle(_bundles(world))

    resp = _install(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["instance"] == _INSTANCE
    assert body["extension"] == _EXTENSION
    assert body["tools"] == ["ping"]
    assert body["agents"] == [_AGENT]

    assert _defined(world)[_INSTANCE] == {
        "extension": _EXTENSION,
        "approval": "outbound",
        "agents": [_AGENT],
    }
    assert not (agent_dir / "connections.toml").exists(), "nothing is written into the agent"
    env = _env_file(world).read_text(encoding="utf-8")
    assert _SENTINEL in env, "the credential belongs in the owner-only env file"

    listing = client.get("/api/connections", headers=_headers("viewer")).json()
    assert listing["connections"] == [
        {
            "instance": _INSTANCE,
            "extension": _EXTENSION,
            # The bundle this test writes declares no display_name, so the row
            # falls back to the coordinate — never to a blank.
            "extension_display_name": _EXTENSION,
            "approval": "outbound",
            "agents": [_AGENT],
        }
    ]

    # The other direction: the agent's own view is the grant read from its side,
    # which is exactly what its connector module will attach at the next start.
    held = client.get(f"/api/agents/{agent_id}/connectors", headers=_headers("viewer")).json()
    assert [row["instance"] for row in held["instances"]] == [_INSTANCE]


def test_a_connection_granted_to_nobody_reaches_nobody(world: Path) -> None:
    """Deny by default, proven through the route rather than asserted about it.

    An operator may connect an account before deciding who gets it, and that
    account must work and serve no one. A listing that showed it under an agent
    anyway would be the failure this whole model exists to remove.
    """
    client, agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))

    assert _install(client, agents=[]).status_code == 200

    deployment = client.get("/api/connections", headers=_headers("viewer")).json()
    assert deployment["connections"][0]["agents"] == []
    held = client.get(f"/api/agents/{agent_id}/connectors", headers=_headers("viewer")).json()
    assert held["instances"] == []


def test_the_install_response_carries_no_credential(world: Path) -> None:
    """Asserted on the serialized body — a dict the test built proves nothing."""
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))

    resp = _install(client)
    assert resp.status_code == 200, resp.text
    assert _SENTINEL not in resp.text
    assert "api_token" not in resp.text


def test_an_unsatisfied_host_prerequisite_is_400_and_writes_nothing(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world), manifest=_MANIFEST_NEEDS_HOST)

    resp = _install(client)
    assert resp.status_code == 400
    body = resp.json()
    assert body["unsatisfied_host"] == [
        {
            "name": "definitely_not_installed_xyz",
            "instruction": "brew install definitely-not-installed-xyz",
            "satisfied": False,
        }
    ]
    assert _defined(world) == {}
    assert not _env_file(world).exists()
    assert _SENTINEL not in resp.text


def test_a_missing_declared_secret_is_422_and_writes_nothing(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))

    resp = _install(client, secrets={})
    assert resp.status_code == 422
    assert "api_token" in resp.json()["error"]
    assert _defined(world) == {}
    assert not _env_file(world).exists()


def test_a_duplicate_instance_is_409(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))
    assert _install(client).status_code == 200

    resp = _install(client)
    assert resp.status_code == 409


def test_an_instance_name_with_a_space_is_400_and_leaves_the_config_byte_identical(
    world: Path,
) -> None:
    """The defect an operator hit: a name with a space became a bare TOML key.

    ``[connections."blackarc industrial email"]`` written unquoted does not
    parse, and that file is now the whole deployment's grant list — so every
    agent loses every connection, not just this one. The bundle here declares NO
    ``[[secrets]]`` on purpose: that is the shape (``google_workspace``,
    ``dropbox``) where nothing ever built a ``SecretRef`` and so nothing ever
    checked the name.
    """
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world), manifest=_MANIFEST_HOSTED)

    resp = _install(client, instance="blackarc industrial email", secrets={})

    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert "blackarc industrial email" in error
    assert "blackarc_industrial_email" in error
    assert _defined(world) == {}


#: A name the coordinate rule refuses. A hyphen is legal in a bare TOML key, so a
#: hand-edited config can contain one; it can never be created through any
#: surface, because the name also becomes an env-var segment where a hyphen is
#: not a legal shell variable name.
_REFUSED_INSTANCE = "personal-dropbox"


def _handwritten_connection(world: Path, instance: str) -> None:
    """A connection block no surface would have written — hand-edited into place."""
    path = ConnectionRegistry(_arc_dir(world)).path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'[connections."{instance}"]\nextension = "{_EXTENSION}"\n'
        f'approval = "outbound"\nagents = ["{_AGENT}"]\n',
        encoding="utf-8",
    )


def test_a_connection_whose_name_the_rule_rejects_can_still_be_removed(
    world: Path,
) -> None:
    """A hand-edited config must never leave an undeletable connection.

    Not legacy accommodation — the name is refused everywhere it could be
    created. This is the exit the strict rule needs so refusing a name can
    never be worse than accepting it.
    """
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world), manifest=_MANIFEST_HOSTED)
    _handwritten_connection(world, _REFUSED_INSTANCE)

    resp = client.delete(f"/api/connections/{_REFUSED_INSTANCE}", headers=_headers())

    assert resp.status_code == 200, resp.text
    assert resp.json()["removed_config"] is True
    assert _REFUSED_INSTANCE not in _defined(world)


def test_a_connection_whose_name_the_rule_rejects_is_still_listed(world: Path) -> None:
    """It has to be visible before an operator can delete it."""
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world), manifest=_MANIFEST_HOSTED)
    _handwritten_connection(world, _REFUSED_INSTANCE)

    resp = client.get("/api/connections", headers=_headers("viewer"))

    assert [row["instance"] for row in resp.json()["connections"]] == [_REFUSED_INSTANCE]


def test_creating_a_new_connection_under_that_same_name_is_still_400(world: Path) -> None:
    """Reading an existing name is not permission to create another one."""
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world), manifest=_MANIFEST_HOSTED)

    resp = _install(client, instance=_REFUSED_INSTANCE, secrets={})

    assert resp.status_code == 400, resp.text
    assert "personal_dropbox" in resp.json()["error"]


def test_an_unknown_extension_is_400(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    resp = _install(client)
    assert resp.status_code == 400
    assert "acme_tickets" in resp.json()["error"]


def test_granting_to_an_agent_this_deployment_does_not_have_is_refused(world: Path) -> None:
    """A grant to a name nothing matches is written, listed, and effective for no one.

    Not a security hole — it hands out nothing. It is the silent no-op that
    leaves an operator staring at a connection their agent still cannot see, so
    it is refused where it is typed, naming the name.
    """
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))

    resp = _install(client, agents=["not_an_agent"])

    assert resp.status_code == 400, resp.text
    assert "not_an_agent" in resp.json()["error"]
    assert _defined(world) == {}


# --- grants: the only thing that decides access ------------------------------


def _grant(client: TestClient, agents: list[str], *, token: str = "operator") -> Any:
    return client.post(
        f"/api/connections/{_INSTANCE}/grant", json={"agents": agents}, headers=_headers(token)
    )


def _revoke(client: TestClient, agents: list[str], *, token: str = "operator") -> Any:
    return client.request(
        "DELETE",
        f"/api/connections/{_INSTANCE}/grant",
        json={"agents": agents},
        headers=_headers(token),
    )


def _second_agent(world: Path, name: str = "second_agent") -> str:
    """Another agent in the same fleet, so a grant to one is provably not a grant to both."""
    agent_dir = arc_team(base=_arc_dir(world)) / name
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(
        f'[agent]\nname = "{name}"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n[security]\ntier = "personal"\n',
        encoding="utf-8",
    )
    return name


async def _open_fake_backend(backend: FakeBackend) -> FakeBackend:
    return backend


async def _start_agent(world: Path, agent: str, backend: FakeBackend) -> ToolRegistry:
    """Start one agent's connectors capability exactly as a running agent starts it.

    This is the far end of the seam. The route writes a grant; THIS is what reads
    it, and what it produces is the catalog the model is offered. Anything short
    of it — the response body, the TOML file, even ``granted_to`` — is a claim
    about an intermediate, and every defect this feature shipped lived in an
    intermediate that was correct.
    """
    did = f"did:arc:arc:exec/{agent}"
    identity = MagicMock()
    identity.did = did
    gate = HumanGate(
        operator_signer=InProcessSigner(bytes(SigningKey.generate())),
        agent_did=did,
        tier="personal",
    )
    registry = ToolRegistry(
        config=ToolsConfig(policy=ToolConfig()),
        bus=ModuleBus(),
        telemetry=MagicMock(),
        human_gate=gate,
    )
    _runtime.reset()
    _runtime.configure(
        config={
            "data_dir": str(world / "data"),
            "extensions_root": str(_bundles(world)),
            "arc_dir": str(_arc_dir(world)),
        },
        telemetry=None,
        workspace=arc_team(base=_arc_dir(world)) / agent / "workspace",
        identity=identity,
        config_path=arc_team(base=_arc_dir(world)) / agent / "arcagent.toml",
        tool_registry=registry,
        tier="personal",
        human_gate=gate,
        arcstore_opener=lambda: _open_fake_backend(backend),
    )
    try:
        await Connectors().setup(None)
    finally:
        _runtime.reset()
    return registry


async def test_granting_through_the_route_registers_the_tools_for_exactly_that_agent(
    world: Path,
) -> None:
    """The grant made in the browser is the grant the running agent enforces.

    Driven all the way through: the route writes it, and the real
    :class:`~arcagent.modules.connectors.capabilities.Connectors` capability then
    starts for each agent and answers with its real ``ToolRegistry``. A route
    that wrote a correct-looking grant into a file the runtime does not read
    would satisfy every assertion about its own body and hand the agent nothing.
    """
    second = _second_agent(world)
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))
    assert _install(client, agents=[]).status_code == 200

    resp = _grant(client, [_AGENT])

    assert resp.status_code == 200, resp.text
    assert resp.json()["agents"] == [_AGENT]
    backend = client.app.state.arcstore_backend
    assert "ping" in (await _start_agent(world, _AGENT, backend)).tools
    assert "ping" not in (await _start_agent(world, second, backend)).tools, (
        "a grant to one agent is not a grant to the fleet"
    )


async def test_revoking_through_the_route_takes_the_tools_away(world: Path) -> None:
    """The mirror, and the half an operator has to be able to trust.

    A revoke that removed the row and left the agent serving the verbs would be
    invisible in every listing and completely ineffective, which is the worst
    shape an access-control change can have.
    """
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))
    assert _install(client).status_code == 200
    backend = client.app.state.arcstore_backend
    assert "ping" in (await _start_agent(world, _AGENT, backend)).tools

    assert _revoke(client, [_AGENT]).status_code == 200

    assert "ping" not in (await _start_agent(world, _AGENT, backend)).tools


def test_the_listing_shows_holders_after_a_grant_and_after_a_revoke(world: Path) -> None:
    """Four agents, three connections, one glance: the operator's actual question.

    'Maybe 2 agents can access jira and 2 don't have the connection.' The listing
    is where that is answered, so it has to be right immediately after both verbs
    — a stale holder list is how an operator concludes a revoke did not work and
    revokes something else.
    """
    second = _second_agent(world)
    client, agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))
    assert _install(client).status_code == 200

    assert _grant(client, [second]).status_code == 200
    listing = client.get("/api/connections", headers=_headers("viewer")).json()
    assert listing["connections"][0]["agents"] == [_AGENT, second]

    assert _revoke(client, [_AGENT]).json()["agents"] == [second]
    listing = client.get("/api/connections", headers=_headers("viewer")).json()
    assert listing["connections"][0]["agents"] == [second]

    # And the agent that lost it says so from its own side.
    held = client.get(f"/api/agents/{agent_id}/connectors", headers=_headers("viewer")).json()
    assert held["instances"] == []


def test_revoking_from_an_agent_that_never_held_it_is_not_an_error(world: Path) -> None:
    """An operator making sure nobody has something must not be stopped by a name
    that already does not."""
    second = _second_agent(world)
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))
    assert _install(client).status_code == 200

    resp = _revoke(client, [second])

    assert resp.status_code == 200, resp.text
    assert resp.json()["agents"] == [_AGENT]


def test_a_grant_naming_no_connection_is_404(world: Path) -> None:
    """Not a 400: a surface distinguishing "no such connection" from "refused"
    branches on the status, not on the message text."""
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))

    assert _grant(client, [_AGENT]).status_code == 404


def test_an_empty_agent_list_is_refused_rather_than_silently_doing_nothing(
    world: Path,
) -> None:
    """A grant that changes nothing and answers 200 reads as a grant that worked."""
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))
    assert _install(client).status_code == 200

    assert _grant(client, []).status_code == 400
    assert _revoke(client, []).status_code == 400


def test_a_viewer_is_refused_grant_and_revoke(world: Path) -> None:
    """Both are access-control decisions, so both are the operator's alone.

    Checked against the registry the runtime reads, because a 403 that had
    already written the grant would be a refusal in the response only.
    """
    second = _second_agent(world)
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))
    assert _install(client).status_code == 200

    assert _grant(client, [second], token="viewer").status_code == 403
    assert _revoke(client, [_AGENT], token="viewer").status_code == 403

    registry = ConnectionRegistry(_arc_dir(world))
    assert registry.granted_to(second) == {}, "a refused grant must not have been written"
    assert list(registry.granted_to(_AGENT)) == [_INSTANCE], "a refused revoke must change nothing"


# --- the rest of the verbs -------------------------------------------------


def test_auth_rotates_and_names_only_the_fields(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))
    assert _install(client).status_code == 200

    rotated = "zzz-rotated-sentinel-8822"
    resp = client.put(
        f"/api/connections/{_INSTANCE}/auth",
        json={"secrets": {"api_token": rotated}},
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.json() == {"instance": _INSTANCE, "updated": ["api_token"]}
    assert rotated not in resp.text
    env = _env_file(world).read_text(encoding="utf-8")
    assert rotated in env
    assert _SENTINEL not in env


def test_the_auth_view_names_the_credential_fields_and_never_a_value(world: Path) -> None:
    """The panel has to know which form to draw before it draws one."""
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))
    assert _install(client).status_code == 200

    resp = client.get(f"/api/connections/{_INSTANCE}/auth", headers=_headers("viewer"))

    assert resp.status_code == 200
    body = resp.json()
    assert [field["name"] for field in body["credentials"]] == ["api_token"]
    assert body["hosts"] == []
    assert body["reachable"] is True
    assert _SENTINEL not in resp.text


def test_the_auth_view_of_a_credential_less_connector_names_the_host_command(
    world: Path,
) -> None:
    """The defect a non-technical operator hit: a Replace-credentials button over
    an empty form.

    Half the shipped bundles declare no ``[[secrets]]`` because their binary keeps
    its own token, so a panel reading only the credential list has nothing to
    render and nothing to say. The command that authorises the binary is what the
    operator actually needs, and it has to reach the browser.
    """
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world), manifest=_MANIFEST_HOSTED)
    assert _install(client, secrets={}).status_code == 200

    resp = client.get(f"/api/connections/{_INSTANCE}/auth", headers=_headers("viewer"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["credentials"] == []
    assert [host["command"] for host in body["hosts"]] == [_AUTHORIZE_COMMAND]


def test_probe_reports_a_live_connection(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))
    assert _install(client).status_code == 200

    resp = client.post(f"/api/connections/{_INSTANCE}/probe", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is True
    assert [tool["name"] for tool in body["tools"]] == ["ping"]


def test_doctor_reports_the_credential_as_present_without_its_value(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))
    assert _install(client).status_code == 200

    resp = client.get(f"/api/connections/{_INSTANCE}/doctor", headers=_headers("viewer"))
    assert resp.status_code == 200
    checks = {row["check"]: row for row in resp.json()["checks"]}
    assert checks["api_token"]["status"] == "present"
    assert checks["connection"]["status"] == "reachable"
    assert _SENTINEL not in resp.text


def test_approve_records_the_contract_served_now(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))
    assert _install(client).status_code == 200

    resp = client.post(f"/api/connections/{_INSTANCE}/approve", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == {"instance": _INSTANCE, "approved": ["ping"]}


def test_remove_drops_the_credential_and_the_config_block(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))
    assert _install(client).status_code == 200

    resp = client.delete(f"/api/connections/{_INSTANCE}", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["removed_secrets"] == ["api_token"]
    assert body["removed_config"] is True
    assert _defined(world) == {}
    assert _SENTINEL not in _env_file(world).read_text(encoding="utf-8")


def test_removing_an_instance_that_does_not_exist_is_not_an_error(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    resp = client.delete("/api/connections/ghost", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["removed_config"] is False
    assert body["removed_secrets"] == []


# --- the operator gate -----------------------------------------------------


def test_a_viewer_is_refused_every_mutation(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))
    viewer = _headers("viewer")

    assert _install(client, token="viewer").status_code == 403
    assert (
        client.put(
            f"/api/connections/{_INSTANCE}/auth",
            json={"secrets": {"api_token": _SENTINEL}},
            headers=viewer,
        ).status_code
        == 403
    )
    assert client.post(f"/api/connections/{_INSTANCE}/probe", headers=viewer).status_code == 403
    assert client.post(f"/api/connections/{_INSTANCE}/approve", headers=viewer).status_code == 403
    assert client.delete(f"/api/connections/{_INSTANCE}", headers=viewer).status_code == 403
    assert _grant(client, [_AGENT], token="viewer").status_code == 403
    assert _revoke(client, [_AGENT], token="viewer").status_code == 403
    assert _defined(world) == {}
    assert not _env_file(world).exists()


def test_an_oversized_install_body_is_413(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))
    resp = client.post(
        "/api/connections",
        content=b'{"extension": "acme_tickets", "instance": "work", "secrets": {"api_token": "'
        + b"x" * 70_000
        + b'"}}',
        headers={**_headers(), "Content-Type": "application/json"},
    )
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# Sign-in: the button must be honest in BOTH directions
# ---------------------------------------------------------------------------


def _auth_status(client: TestClient, *, token: str = "viewer") -> Any:
    return client.get(f"/api/connections/{_INSTANCE}/auth-status", headers=_headers(token))


def _authorize(client: TestClient, body: dict[str, Any], *, token: str = "operator") -> Any:
    return client.post(
        f"/api/connections/{_INSTANCE}/authorize",
        json=body,
        headers=_headers(token),
    )


def _connect_signin(
    world: Path,
    *,
    token_login: bool,
    verify: bool = True,
    entrypoint: str = "acme_signin_attachment",
) -> tuple[TestClient, str, Path, Path]:
    """An installed connector whose binary holds its own credential.

    ``token_login`` picks which of the two real shapes it is: a binary that
    completes its login from stdin, or one whose login only a person can finish.
    ``verify`` picks whether its manifest declares the command that proves a
    sign-in; ``entrypoint`` picks whether the probe tracks the sign-in or always
    answers yes, which is the ``dbxcli version`` shape.
    """
    client, agent_id, agent_dir = _agent(world)
    bundle = _write_bundle(_bundles(world), manifest=_MANIFEST)
    manifest = _signin_manifest(
        host=sys.executable,
        authorize=f"{sys.executable} -m acme_login",
        token_login=_token_login_command(bundle) if token_login else "",
        verify=_verify_command(bundle) if verify else "",
        entrypoint=entrypoint,
    )
    (bundle / "extension.toml").write_text(manifest, encoding="utf-8")
    (bundle / _SIGNED_IN_MARKER).write_text("ok", encoding="utf-8")
    resp = _install(client, secrets={})
    assert resp.status_code == 200, resp.text
    (bundle / _SIGNED_IN_MARKER).unlink()
    return client, agent_id, agent_dir, bundle


def test_auth_status_reports_the_real_state_of_an_interactive_only_connector(
    world: Path,
) -> None:
    """Readable by a viewer, and it reports the AUTHORISATION CHECK — not a
    stored flag, and not the probe.

    The connector is not signed in, so ``sign_in`` is ``signed_out`` and the
    answer carries the exact command a person runs on this host. That command is
    the honest dead end the panel renders.
    """
    client, _agent_id, _dir, _bundle = _connect_signin(world, token_login=False)

    resp = _auth_status(client)

    assert resp.status_code == 200
    body = resp.json()
    assert body["sign_in"] == "signed_out"
    assert body["detail"]
    assert body["command"] == f"{sys.executable} -m acme_login"


def test_auth_status_offers_no_terminal_command_when_arc_can_sign_in_itself(
    world: Path,
) -> None:
    """The mirror-image lie: telling an operator to open a terminal for a login
    Arc can finish sends them away from the button that works."""
    client, _agent_id, _dir, _bundle = _connect_signin(world, token_login=True)

    body = _auth_status(client).json()

    assert body["sign_in"] == "signed_out"
    assert body["command"] == ""


def test_auth_status_reports_a_signed_in_connector_as_authorized(world: Path) -> None:
    client, _agent_id, _dir, bundle = _connect_signin(world, token_login=True)
    (bundle / _SIGNED_IN_MARKER).write_text("ok", encoding="utf-8")

    body = _auth_status(client).json()

    assert body["sign_in"] == "signed_in"


def test_a_binary_that_runs_but_is_signed_out_is_never_reported_as_signed_in(
    world: Path,
) -> None:
    """The shipped defect, reproduced exactly.

    ``dbxcli`` declares ``probe_argv = ["version"]``, so it answers happily with
    no saved credentials at all — and that answer was rendered to the operator as
    a green tick reading **Signed in — dbxcli version: 3.7.1**. Here the probe is
    the always-reachable one and the authorisation check fails: reachable must
    stay true and the sign-in must read ``signed_out``.
    """
    client, _agent_id, _dir, _bundle = _connect_signin(
        world, token_login=False, entrypoint="acme_hosted_attachment"
    )

    body = _auth_status(client).json()

    assert body["reachable"] is True
    assert body["sign_in"] == "signed_out"


def test_doctor_reports_the_sign_in_separately_from_the_connection(world: Path) -> None:
    """Doctor is where an operator goes to find out what is wrong.

    A connection that answers but has no account signed in is precisely that, so
    the two facts get two rows: a single "reachable" row read as "all fine" is
    the same conflation the panel made.
    """
    client, _agent_id, _dir, _bundle = _connect_signin(
        world, token_login=False, entrypoint="acme_hosted_attachment"
    )

    checks = client.get(f"/api/connections/{_INSTANCE}/doctor", headers=_headers("viewer")).json()[
        "checks"
    ]

    rows = {row["check"]: row["status"] for row in checks}
    assert rows["connection"] == "reachable"
    assert rows["sign-in"] == "signed_out"


def test_a_bundle_declaring_no_authorisation_check_reports_unknown(world: Path) -> None:
    """Not known is the honest answer. Reported as signed in it is the defect;
    reported as signed out it sends an operator to redo a login already done."""
    client, _agent_id, _dir, _bundle = _connect_signin(
        world, token_login=False, verify=False, entrypoint="acme_hosted_attachment"
    )

    body = _auth_status(client).json()

    assert body["reachable"] is True
    assert body["sign_in"] == "unknown"


def test_the_evidence_line_names_the_command_that_was_actually_run(world: Path) -> None:
    """The panel prints this next to "Signed in", so it has to be the evidence.

    It used to be the probe's own line, which for a connector Arc cannot log in
    read "signs in on this host; Arc ran nothing" — offered inside a green box as
    the proof that it was signed in. Nothing was run and nothing was checked, so
    the sentence and the badge contradicted each other.
    """
    client, _agent_id, _dir, bundle = _connect_signin(world, token_login=False)
    (bundle / _SIGNED_IN_MARKER).write_text("ok", encoding="utf-8")

    body = _auth_status(client).json()

    assert body["sign_in"] == "signed_in"
    assert body["detail"].startswith(_verify_command(bundle))
    assert "Arc ran nothing" not in body["detail"]


def test_an_unknown_sign_in_offers_no_evidence_at_all(world: Path) -> None:
    """Nothing was checked, so there is nothing to show. A probe line here would
    be evidence for a question it does not answer."""
    client, _agent_id, _dir, _bundle = _connect_signin(
        world, token_login=False, verify=False, entrypoint="acme_hosted_attachment"
    )

    assert _auth_status(client).json()["detail"] == ""


def test_authorize_with_a_token_runs_the_login_and_reports_the_real_result(
    world: Path,
) -> None:
    """The token reaches the binary's stdin and the connection becomes reachable.

    Asserted through the marker the login itself writes, so a route that stored
    the token, or answered without running anything, fails here.
    """
    client, _agent_id, _dir, bundle = _connect_signin(world, token_login=True)

    resp = _authorize(client, {"token": _SENTINEL})

    assert resp.status_code == 200
    assert (bundle / _SIGNED_IN_MARKER).exists()
    assert resp.json()["sign_in"] == "signed_in"


def test_authorize_never_returns_the_token(world: Path) -> None:
    """The response body is rendered in a browser and cached by the query client.

    The sign-in is asserted to have really happened, so this cannot pass by the
    route having done nothing with the token at all.
    """
    client, _agent_id, _dir, bundle = _connect_signin(world, token_login=True)

    resp = _authorize(client, {"token": _SENTINEL})

    assert (bundle / _SIGNED_IN_MARKER).exists()
    assert _SENTINEL not in resp.text


def test_authorize_never_logs_the_token(world: Path, caplog: pytest.LogCaptureFixture) -> None:
    client, _agent_id, _dir, bundle = _connect_signin(world, token_login=True)

    with caplog.at_level("DEBUG"):
        _authorize(client, {"token": _SENTINEL})

    assert (bundle / _SIGNED_IN_MARKER).exists()
    assert _SENTINEL not in caplog.text


def test_authorize_never_writes_the_token_into_the_agents_world(world: Path) -> None:
    """A host binary owns its own credential; Arc storing a copy would be a
    second place to leak it from and a second place to forget to remove it."""
    client, _agent_id, agent_dir, _bundle = _connect_signin(world, token_login=True)

    _authorize(client, {"token": _SENTINEL})

    leaked = [
        path
        for path in agent_dir.rglob("*")
        if path.is_file() and _SENTINEL in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert leaked == []


def test_authorize_on_an_interactive_only_connector_claims_nothing_and_runs_nothing(
    world: Path,
) -> None:
    """The whole constraint: a button that claims to have finished a browser
    hand-off is worse than no button, because the operator stops looking."""
    client, _agent_id, _dir, bundle = _connect_signin(world, token_login=False)

    resp = _authorize(client, {"token": _SENTINEL})

    assert resp.status_code == 200
    body = resp.json()
    assert body["sign_in"] == "signed_out"
    assert body["command"] == f"{sys.executable} -m acme_login"
    assert not (bundle / _SIGNED_IN_MARKER).exists()


def test_authorize_without_a_token_does_not_pretend_to_have_signed_in(world: Path) -> None:
    client, _agent_id, _dir, bundle = _connect_signin(world, token_login=True)

    body = _authorize(client, {}).json()

    assert body["sign_in"] == "signed_out"
    assert not (bundle / _SIGNED_IN_MARKER).exists()


def test_auth_status_for_an_instance_that_is_not_connected_is_404(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))

    assert _auth_status(client).status_code == 404
    assert _authorize(client, {}).status_code == 404


# ---------------------------------------------------------------------------
# Host setup: verified before anything runs, or it does not happen
# ---------------------------------------------------------------------------

_HELPER = "acme_helper_xyz"

_HELPER_BODY = b"#!/bin/sh\necho acme 1.0.0\n"


def _helper_tarball(member: str = f"acme_1.0.0/{_HELPER}") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(member)
        info.size = len(_HELPER_BODY)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(_HELPER_BODY))
    return buffer.getvalue()


def _host_setup_manifest(
    *, platform: str, digest: str, member: str = f"acme_1.0.0/{_HELPER}"
) -> str:
    return f"""
[extension]
name = "{_EXTENSION}"
version = "1.0.0"
attachment = "native"
description = "Acme through a helper this host does not have yet."

[config.native]
entrypoint = "acme_hosted_attachment"

[artifact]
package = "acme/acme"
version = "1.0.0"

[artifact.platforms."{platform}"]
url = "https://example.invalid/acme_1.0.0.tar.gz"
sha256 = "{digest}"
member = "{member}"

[[host_requires]]
name = "{_HELPER}"
authorize_command = "{_HELPER} login"
instruction = "Download acme_1.0.0.tar.gz, check it against the release SHA256SUMS, and install it."

[tools]
allow = ["ping"]

[[tools.declared]]
name = "ping"
description = "Report the Acme client version."
classification = "read_only"

[approval]
default = "outbound"
"""


@pytest.fixture
def host_setup(world: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A home directory and a PATH that exist only for this test.

    ``HOME`` is redirected so the route's real default install directory —
    ``~/.local/bin``, resolved by the shipped code and not by the test — lands
    inside ``tmp_path``. Nothing in this suite may write to the developer's own.
    """
    home = tmp_path / "home"
    home.mkdir()
    install_dir = home / ".local" / "bin"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{install_dir}:{tmp_path / 'empty-path'}")
    return install_dir


def _setup_host(client: TestClient, *, token: str = "operator") -> Any:
    return client.post(f"/api/connections/{_EXTENSION}/host-setup", headers=_headers(token))


def _serving(payload: bytes, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the shipped HTTPS transport, and nothing else about the install.

    The pin resolution, the digest check, the unpack, and the placement all still
    run for real — substituting the install itself would prove only that the
    route can call a substitute.
    """
    from arcagent.extension import host_install

    requested: list[str] = []

    async def fetch(url: str) -> bytes:
        requested.append(url)
        return payload

    monkeypatch.setattr(host_install, "https_get", fetch)
    return requested


def test_host_setup_installs_a_verified_binary_into_the_user_writable_directory(
    world: Path, host_setup: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _helper_tarball()
    _serving(payload, monkeypatch)
    client, _agent_id, _dir = _agent(world)
    _write_bundle(
        _bundles(world),
        manifest=_host_setup_manifest(
            platform=host_platform(), digest=hashlib.sha256(payload).hexdigest()
        ),
    )

    resp = _setup_host(client)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["installed"] is True, body
    assert (host_setup / _HELPER).read_bytes() == _HELPER_BODY


def test_host_setup_refuses_a_digest_mismatch_and_installs_nothing(
    world: Path, host_setup: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A digest mismatch is a hard refusal, never a warning — and the operator is
    handed the manual steps rather than a dead end."""
    _serving(_helper_tarball(), monkeypatch)
    client, _agent_id, _dir = _agent(world)
    _write_bundle(
        _bundles(world),
        manifest=_host_setup_manifest(platform=host_platform(), digest="b" * 64),
    )

    body = _setup_host(client).json()

    assert body["installed"] is False
    assert not host_setup.exists() or list(host_setup.iterdir()) == []
    assert "SHA256SUMS" in body["manual_steps"]


def test_host_setup_refuses_a_platform_with_no_pinned_digest_without_downloading(
    world: Path, host_setup: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing unverified because THIS platform was not pinned is the exact
    supply-chain hole a per-platform digest exists to close."""
    requested = _serving(_helper_tarball(), monkeypatch)
    client, _agent_id, _dir = _agent(world)
    _write_bundle(
        _bundles(world),
        manifest=_host_setup_manifest(platform="sunos/sparc", digest="c" * 64),
    )

    body = _setup_host(client).json()

    assert body["installed"] is False
    assert requested == []
    assert not host_setup.exists() or list(host_setup.iterdir()) == []
    assert body["manual_steps"]


def test_host_setup_never_writes_outside_the_install_directory(
    world: Path, host_setup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``member`` is a path a manifest chose, so it is placed by basename alone."""
    payload = _helper_tarball(member=f"../../../../{_HELPER}")
    _serving(payload, monkeypatch)
    client, _agent_id, _dir = _agent(world)
    _write_bundle(
        _bundles(world),
        manifest=_host_setup_manifest(
            platform=host_platform(),
            digest=hashlib.sha256(payload).hexdigest(),
            member=f"../../../../{_HELPER}",
        ),
    )

    _setup_host(client)

    assert (host_setup / _HELPER).is_file()
    assert not (tmp_path / _HELPER).exists()
    assert not (host_setup.parent.parent / _HELPER).exists()


def test_host_setup_reports_a_bundle_with_nothing_to_install_rather_than_failing(
    world: Path, host_setup: Path
) -> None:
    """``jira`` and ``confluence`` need no host binary at all; the button must
    say so rather than 500 or claim to have installed something."""
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))

    body = _setup_host(client).json()

    assert body["installed"] is True
    assert body["detail"]


def test_host_setup_refuses_a_bundle_that_does_not_exist(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))

    resp = client.post("/api/connections/nosuchbundle/host-setup", headers=_headers())

    assert resp.status_code == 400


def test_a_viewer_is_refused_the_sign_in_and_the_host_install(world: Path) -> None:
    """Both mutations reach the host: one runs a program, the other puts one there."""
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))

    assert _authorize(client, {"token": _SENTINEL}, token="viewer").status_code == 403
    assert _setup_host(client, token="viewer").status_code == 403


# ---------------------------------------------------------------------------
# The catalog answers about THIS host, not only about the manifest
# ---------------------------------------------------------------------------


def _catalog_with_director(
    world: Path,
    monkeypatch: pytest.MonkeyPatch,
    present: set[str],
    manifest: str,
) -> dict[str, Any]:
    """Read the catalog with host presence decided by the test, not the machine."""
    fleet = world / "fleet_extensions"
    _write_bundle(fleet, manifest=manifest)
    monkeypatch.setenv("ARC_EXTENSIONS_ROOT", str(fleet))
    client, _agent_id, _dir = _agent(world)
    client.app.state.host_director = HostPrerequisiteDirector(  # type: ignore[attr-defined]
        path_lookup=lambda name: f"/usr/bin/{name}" if name in present else None
    )
    resp = client.get("/api/connectors/catalog", headers=_headers("viewer"))
    assert resp.status_code == 200
    entry: dict[str, Any] = next(e for e in resp.json()["available"] if e["name"] == _EXTENSION)
    return entry


def test_a_prerequisite_this_host_already_has_reports_satisfied(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: a card told an operator to install a binary already present.

    The catalog used to echo the manifest's ``[[host_requires]]`` verbatim, so a
    bundle needing ``dbxcli`` showed its full install instructions forever — on a
    host where ``dbxcli`` had been installed for hours.
    """
    entry = _catalog_with_director(world, monkeypatch, {"sh"}, _MANIFEST_HOSTED)

    required = entry["host_requires"]
    assert [r["name"] for r in required] == ["sh"]
    assert required[0]["satisfied"] is True
    # Nothing left for the operator to do, so nothing is asked of them.
    assert required[0]["instruction"] == ""


def test_a_prerequisite_this_host_lacks_reports_unsatisfied_with_its_instruction(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _catalog_with_director(world, monkeypatch, set(), _MANIFEST_HOSTED)

    required = entry["host_requires"]
    assert required[0]["satisfied"] is False
    assert "Install the acme CLI" in required[0]["instruction"]


# --- native OAuth connect through the dashboard (SPEC-062) ---------------------

_MANIFEST_OAUTH = """
[extension]
name = "acme_oauth"
version = "1.0.0"
attachment = "native"
description = "Acme via a native OAuth code exchange."

[config.native]
entrypoint = "acme_oauth_attachment"

[[secrets]]
name = "app_key"
sensitive = false

[[secrets]]
name = "app_secret"

[[secrets]]
name = "refresh_token"

[oauth]
authorize_url = "https://provider.example/oauth2/authorize"
token_url = "https://provider.example/oauth2/token"
client_id_secret = "app_key"
client_secret_secret = "app_secret"
refresh_token_secret = "refresh_token"
authorize_params = { token_access_type = "offline" }

[tools]
allow = ["ping"]

[[tools.declared]]
name = "ping"
description = "ping"
classification = "read_only"

[approval]
default = "outbound"
"""

_ADAPTER_OAUTH = '''
"""A native OAuth connector: reachable once the exchanged refresh token arrives."""

from __future__ import annotations

from typing import Any

from arcagent.extension.attachment import ProbeResult, ToolResult, ToolSpec


class OAuthAttachment:
    def __init__(self, context: dict[str, Any]) -> None:
        self._has_token = bool(context.get("refresh_token"))

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        held = "authenticated" if self._has_token else "unauthenticated"
        return ProbeResult(reachable=True, tools=await self.describe_tools(), detail=f"acme oauth ({held})")

    async def describe_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="ping",
                description="ping",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                classification="read_only",
            )
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, content="ok")


def build_native_attachment(context: dict[str, Any]) -> OAuthAttachment:
    return OAuthAttachment(context)
'''


def _write_oauth_bundle(world: Path) -> None:
    bundle = _bundles(world) / "acme_oauth"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "extension.toml").write_text(_MANIFEST_OAUTH, encoding="utf-8")
    (bundle / "acme_oauth_attachment.py").write_text(_ADAPTER_OAUTH, encoding="utf-8")


def test_the_dashboard_completes_a_native_oauth_connection(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole connect flow a non-technical operator drives from arcui: install
    with the app key/secret, read the authorize URL, paste the code, and the
    dashboard exchanges it for a DURABLE refresh token — no token ever typed."""
    client, _agent_id, _dir = _agent(world)
    _write_oauth_bundle(world)

    installed = client.post(
        "/api/connections",
        json={
            "extension": "acme_oauth",
            "instance": "obx",
            "agents": [_AGENT],
            "secrets": {"app_key": "ak-9", "app_secret": "as-9"},
        },
        headers=_headers("operator"),
    )
    assert installed.status_code == 200, installed.text

    auth = client.get("/api/connections/obx/auth", headers=_headers("viewer")).json()
    assert auth["oauth"] is True, "the panel is told to show the URL flow, not a token form"
    assert "ak-9" in auth["authorize_url"]
    assert "token_access_type=offline" in auth["authorize_url"]
    assert "refresh_token" not in {c["name"] for c in auth["credentials"]}

    async def _fake_post(
        url: str, data: dict[str, str], creds: tuple[str, str]
    ) -> tuple[int, Any]:
        return 200, {"refresh_token": "rt-web-durable", "expires_in": 14400}

    monkeypatch.setattr("arcagent.connections._oauth_post", _fake_post)

    done = client.post(
        "/api/connections/obx/oauth", json={"code": "one-time"}, headers=_headers("operator")
    )
    assert done.status_code == 200, done.text
    assert "rt-web-durable" in _env_file(world).read_text(encoding="utf-8"), (
        "the exchanged durable token is persisted to the owner-only env file"
    )


def test_completing_oauth_is_operator_only_and_needs_a_code(
    world: Path,
) -> None:
    """A viewer cannot finish a sign-in, and an empty code is refused before anything runs."""
    client, _agent_id, _dir = _agent(world)
    _write_oauth_bundle(world)
    client.post(
        "/api/connections",
        json={
            "extension": "acme_oauth",
            "instance": "obx",
            "agents": [_AGENT],
            "secrets": {"app_key": "ak-9", "app_secret": "as-9"},
        },
        headers=_headers("operator"),
    )

    assert (
        client.post(
            "/api/connections/obx/oauth", json={"code": "x"}, headers=_headers("viewer")
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/connections/obx/oauth", json={"code": "  "}, headers=_headers("operator")
        ).status_code
        == 400
    )
