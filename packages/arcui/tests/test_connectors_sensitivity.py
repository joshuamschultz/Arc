"""The connect form's two field kinds, proved at the HTTP boundary.

The Jira panel masked all three of its fields. Two are configuration — an account
address and a base URL — and hiding them protected nothing while removing the one
check an operator could make before the probe ran. The bundle now says which of
its fields are credentials, and this is the assertion that the browser is told.

The test that carries the weight is
``test_the_credential_is_absent_from_the_same_response_that_carries_the_url``.
Both kinds of field are in ONE response, so the URL proves the read-back path
actually ran and the token proves it did not run for a credential. A suite that
only checked an all-sensitive connection would pass against an implementation
that read every value and forgot to filter, because there would be nothing in the
answer to contradict it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from arcgateway import team_roster
from arctrust.identity import AgentIdentity
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.connectors import routes as connector_routes

_SENSITIVE = "zzz-web-sensitive-sentinel-3390"
#: What the operator really typed on the Jira form — no scheme, because that is
#: what a browser bar shows a person — and what it must be stored as.
_TYPED = "ctgfederal.com.atlassian.net"
_PUBLIC = f"https://{_TYPED}"

_EXTENSION = "acme_fields"
_INSTANCE = "work"

_MANIFEST = f"""
[extension]
name = "{_EXTENSION}"
version = "1.0.0"
attachment = "native"
description = "Acme, configured with a URL and authorised with a token."

[config.native]
entrypoint = "acme_fields_web_attachment"

[[secrets]]
name = "api_token"
prompt = "Acme API token, from the Acme console under Settings then API tokens."

[[secrets]]
name = "base_url"
prompt = "Your Acme web address, exactly as it appears in the browser bar."
sensitive = false
format = "https_url"

[tools]
allow = ["ping"]

[[tools.declared]]
name = "ping"
description = "Report the Acme client version."
classification = "read_only"

[approval]
default = "outbound"
"""

#: The entrypoint name is unique to this suite: a native entrypoint is imported by
#: bare module name and ``sys.modules`` caches it for the session, so two fixtures
#: sharing a name would silently serve each other's implementation.
_ADAPTER = '''
"""The acme fixture's own implementation, outside every Arc package."""

from __future__ import annotations

from typing import Any

from arcagent.extension.attachment import ProbeResult, ToolResult, ToolSpec


class AcmeFieldsAttachment:
    def __init__(self, context: dict[str, Any]) -> None:
        self._token = str(context.get("api_token") or "")

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        if not self._token:
            return ProbeResult(reachable=False, detail="acme has no credential for api_token")
        return ProbeResult(reachable=True, tools=await self.describe_tools(), detail="ok")

    async def describe_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="ping", description="Report the Acme client version.")]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, content="acme 1.0.0")


def build_native_attachment(context: dict[str, Any]) -> AcmeFieldsAttachment:
    return AcmeFieldsAttachment(context)
'''


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ARC_EXTENSIONS_ROOT", raising=False)
    return tmp_path


#: The agent directory name — the coordinate a grant is written against.
_AGENT = "acme_agent"


def _arc_dir(world: Path) -> Path:
    """The deployment root: connections, credentials and the bundle search path."""
    return world / "arc"


def _bundles(world: Path) -> Path:
    return _arc_dir(world) / "extensions"


def _env_file(world: Path) -> Path:
    return _arc_dir(world) / "connections.env"


def _agent(world: Path) -> tuple[TestClient, str, Path]:
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    key_dir = world / "keys"
    identity.save_keys(key_dir)
    team_root = _arc_dir(world) / "team"
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


def _write_bundle_at(root: Path) -> Path:
    """Write the bundle into one search root — the deployment's, or a fleet override."""
    bundle = root / _EXTENSION
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "extension.toml").write_text(_MANIFEST, encoding="utf-8")
    (bundle / "acme_fields_web_attachment.py").write_text(_ADAPTER, encoding="utf-8")
    return bundle


def _headers(token: str = "operator") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _install(client: TestClient, secrets: dict[str, str] | None = None) -> Any:
    return client.post(
        "/api/connections",
        json={
            "extension": _EXTENSION,
            "instance": _INSTANCE,
            "agents": [_AGENT],
            "secrets": {"api_token": _SENSITIVE, "base_url": _TYPED}
            if secrets is None
            else secrets,
        },
        headers=_headers(),
    )


def _connected(world: Path) -> tuple[TestClient, str, Path]:
    client, agent_id, agent_dir = _agent(world)
    _write_bundle_at(_bundles(world))
    assert _install(client).status_code == 200
    return client, agent_id, agent_dir


def _fields(body: Any) -> dict[str, Any]:
    return {field["name"]: field for field in body}


def test_the_catalog_tells_the_form_which_fields_to_mask(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install form is drawn from the catalog, before anything is connected.

    The catalog answers the fleet-wide question — which bundles exist on this
    machine — so the bundle goes on the fleet search path rather than inside one
    agent's own directory.
    """
    fleet = world / "fleet_extensions"
    _write_bundle_at(fleet)
    monkeypatch.setenv("ARC_EXTENSIONS_ROOT", str(fleet))
    client, _agent_id, _dir = _agent(world)

    body = client.get("/api/connectors/catalog", headers=_headers("viewer")).json()
    entry = next(e for e in body["available"] if e["name"] == _EXTENSION)
    fields = _fields(entry["secrets"])

    assert fields["api_token"]["sensitive"] is True
    assert fields["base_url"]["sensitive"] is False
    # Nothing is connected, so there is nothing configured to show either way.
    assert all(field["value"] == "" for field in entry["secrets"])


def test_the_credential_is_absent_from_the_same_response_that_carries_the_url(
    world: Path,
) -> None:
    """One response, both kinds — which is what makes each half of it mean something.

    The URL coming back proves the read-back path ran at all. The token not
    coming back therefore proves the filter, rather than proving that no value was
    ever read.
    """
    client, _agent_id, _dir = _connected(world)

    resp = client.get(f"/api/connections/{_INSTANCE}/auth", headers=_headers("viewer"))
    fields = _fields(resp.json()["credentials"])

    assert fields["base_url"]["value"] == _PUBLIC
    assert fields["api_token"]["value"] == ""
    assert _SENSITIVE not in resp.text


def test_no_other_verb_carries_either_value_and_none_carries_the_credential(
    world: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The narrowing is one field on one verb, not a general loosening.

    ``doctor`` reports whether a credential is present and must keep saying only
    that; ``auth-status`` reports a sign-in. Neither has a reason to carry a
    configured value, and the credential appears in none of them, nor in a log.
    """
    client, agent_id, _dir = _connected(world)

    with caplog.at_level(logging.DEBUG):
        doctor = client.get(f"/api/connections/{_INSTANCE}/doctor", headers=_headers("viewer"))
        status = client.get(
            f"/api/connections/{_INSTANCE}/auth-status",
            headers=_headers("viewer"),
        )
        listing = client.get(f"/api/agents/{agent_id}/connectors", headers=_headers("viewer"))

    for resp in (doctor, status, listing):
        assert _SENSITIVE not in resp.text
        assert _PUBLIC not in resp.text
    assert _SENSITIVE not in "".join(record.getMessage() for record in caplog.records)


def test_replacing_only_the_credential_leaves_the_configuration_alone(world: Path) -> None:
    """What the read-back is FOR: a rotation that does not retype the URL.

    ``reauth`` writes only the fields it is given, so a form prefilled from the
    verb above can send the token the operator changed and the URL they did not,
    and the connection keeps working.
    """
    client, _agent_id, _dir = _connected(world)

    updated = client.put(
        f"/api/connections/{_INSTANCE}/auth",
        json={"secrets": {"api_token": "zzz-rotated-8811"}},
        headers=_headers(),
    )

    assert updated.status_code == 200
    assert updated.json()["updated"] == ["api_token"]
    body = client.get(f"/api/connections/{_INSTANCE}/auth", headers=_headers("viewer")).json()
    assert _fields(body["credentials"])["base_url"]["value"] == _PUBLIC


# --- the address a person types ----------------------------------------------


def test_the_address_the_operator_typed_installs_and_is_stored_usable(world: Path) -> None:
    """The reported failure, driven through the web from the exact input.

    ``ctgfederal.com.atlassian.net`` produced *"probe: jira did not answer — …
    Request URL is missing an 'http://' or 'https://' protocol"*. It now installs,
    and the value that comes back is the one the connector will actually use — so
    the operator can see what Arc made of what they typed.
    """
    client, _agent_id, _dir = _connected(world)

    body = client.get(f"/api/connections/{_INSTANCE}/auth", headers=_headers("viewer")).json()

    assert _fields(body["credentials"])["base_url"]["value"] == f"https://{_TYPED}"


def test_a_plaintext_address_is_refused_in_words_the_operator_can_act_on(world: Path) -> None:
    """Refused on the form, before anything is written — not at probe afterwards.

    And in Arc's own sentence: the httpx line that reached the operator named a
    protocol and a "Request URL", neither of which tells them what to change.
    """
    client, _agent_id, _dir = _agent(world)
    _write_bundle_at(_bundles(world))

    resp = _install(client, secrets={"api_token": _SENSITIVE, "base_url": f"http://{_TYPED}"})

    assert resp.status_code == 400
    error = resp.json()["error"]
    assert "base_url" in error
    assert "https://" in error
    assert "Request URL" not in error, "that phrasing is httpx's, not ours"
    assert not _env_file(world).exists(), "a refused install must write nothing"


def test_rotating_the_token_does_not_have_to_retype_the_address(world: Path) -> None:
    """The payoff of showing it: send back exactly what the form was prefilled with.

    A rotation posts the URL it read back, unchanged. It must be accepted and
    shaped identically, or the second save of an unchanged field would refuse.
    """
    client, _agent_id, _dir = _connected(world)

    updated = client.put(
        f"/api/connections/{_INSTANCE}/auth",
        json={"secrets": {"api_token": "zzz-rotated-9902", "base_url": _PUBLIC}},
        headers=_headers(),
    )

    assert updated.status_code == 200
    assert sorted(updated.json()["updated"]) == ["api_token", "base_url"]
    body = client.get(f"/api/connections/{_INSTANCE}/auth", headers=_headers("viewer")).json()
    assert _fields(body["credentials"])["base_url"]["value"] == _PUBLIC
