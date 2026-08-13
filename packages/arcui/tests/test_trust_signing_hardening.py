"""Only the operator can sign — the "no one else can do it" half of SPEC-066.

``POST /api/trust/approve`` mints trust: it signs an artifact with the
deployment operator key and pins that key as a capability verifier, after which
the loader will execute those bytes inside an agent. Every other control in Arc
assumes that surface is unreachable to anyone but the human at the console.

The routes now resolve targets from the FULL inventory so an operator can
re-sign something that already loads. That is a deliberate widening of *what*
can be signed, and it must not have widened *who* can sign it — so the checks
below are written to fail if any of five properties is lost:

H1  Signing is operator-role only, and a refusal writes NOTHING.
H2  Signing is reachable from no tool registry and no agent path (REQ-321).
H3  The operator key never appears in a response body or a log line.
H4  The operator key file cannot be fetched through any serving route.
H5  The agent's own file tools cannot write the operator dir or a foreign
    ``.arcsig``.

Every assertion is made against the DISK or the real app, never against a
status code alone: a route that returned 403 and signed anyway would pass a
response-only test, and that is precisely the failure worth catching.
"""

from __future__ import annotations

import ast
import base64
import logging
from pathlib import Path
from typing import Any

import pytest
from arcagent.capabilities import artifact_signing
from arcgateway import team_roster
from arctrust import OperatorKey, default_operator_key_path
from arctrust.identity import AgentIdentity
from starlette.applications import Starlette
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.trust import routes as trust_routes

_ARCUI_SRC = Path(__file__).resolve().parents[1] / "src" / "arcui"
_ARCAGENT_SRC = Path(__file__).resolve().parents[3] / "arcagent" / "src" / "arcagent"

_VALID_SKILL = (
    "---\n"
    "name: reporter\n"
    "version: 2.0.0\n"
    "description: does reporter\n"
    "triggers: [reporter]\n"
    "tools: [reload]\n"
    "---\n"
    "\n## Resources\n\n## Contract\n\n## Knowledge\n\n## Steps\n\n"
    "## Anti Patterns\n\n## Examples\n\n## Validation\n"
)

_VIEWER = {"Authorization": "Bearer viewer"}
_OPERATOR = {"Authorization": "Bearer operator"}


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin arc home at the test tmp so nothing here touches the real ~/.arc."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))


def _operator_seed(tmp_path: Path) -> bytes:
    """Provision the deployment operator key and hand back its PRIVATE seed.

    Returned so H3/H4 can search for the real bytes rather than a placeholder —
    a leak test that greps for a string the process never held proves nothing.
    """
    key_path = tmp_path / "arc" / "operator" / "operator.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = OperatorKey.load(key_path, generate_if_absent=True)
    seed: bytes = key.seed
    return seed


def _build_agent(team_root: Path, name: str, *, tier: str, sign: bool) -> str:
    """One agent with a ``reporter`` skill. Returns the agent's own DID."""
    agent_dir = team_root / name
    skills = agent_dir / "workspace" / "capabilities" / "skills"
    skills.mkdir(parents=True)
    key_dir = team_root / f"{name}-keys"
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    identity.save_keys(key_dir)

    folder = skills / "reporter"
    folder.mkdir()
    skill_md = folder / "SKILL.md"
    content = _VALID_SKILL.encode("utf-8")
    skill_md.write_bytes(content)
    if sign:
        artifact_signing.write_signature(
            skill_md, content, signer_did=identity.did, private_key=identity.signing_seed
        )
    (agent_dir / "arcagent.toml").write_text(
        f'[agent]\nname = "{name}"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n'
        '[llm]\nmodel = "test/model"\n'
        f'[security]\ntier = "{tier}"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n',
        encoding="utf-8",
    )
    return identity.did


def _make_client(team_root: Path) -> TestClient:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=trust_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = None
    app.state.audit_worm = None
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    return TestClient(app)


def _skill_md(team_root: Path, name: str) -> Path:
    return team_root / name / "workspace" / "capabilities" / "skills" / "reporter" / "SKILL.md"


def _signatures_under(team_root: Path) -> list[Path]:
    """Every ``.arcsig`` anywhere below ``team_root``.

    Deliberately a whole-tree sweep rather than a lookup at the expected
    sidecar path: a refused approval that wrote a signature ANYWHERE is a
    finding, including somewhere the test did not think to look.
    """
    return sorted(team_root.rglob(f"*{artifact_signing.SIDECAR_SUFFIX}"))


# ─────────────────────────────────────────────────────────────────────────────
# H1 — Signing is operator-role only, and a refusal writes nothing
# ─────────────────────────────────────────────────────────────────────────────


def test_h1_a_viewer_cannot_sign_and_nothing_reaches_the_disk(tmp_path: Path) -> None:
    _operator_seed(tmp_path)
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)

    resp = client.post(
        "/api/trust/approve", headers=_VIEWER, json={"agent_id": "olivia", "name": "reporter"}
    )

    assert resp.status_code == 403
    assert _signatures_under(team_root) == []


def test_h1_a_viewer_cannot_resign_an_artifact_that_already_loads(tmp_path: Path) -> None:
    """The route now reaches loaded artifacts. A viewer still must not.

    This is the property the include-loaded widening could have broken, and the
    one a "no ``.arcsig`` on disk" assertion cannot see — the sidecar is already
    there. So the check is that its BYTES do not move: a re-sign would rewrite
    the signature over the same artifact and leave the file present either way.
    """
    _operator_seed(tmp_path)
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="personal", sign=True)
    client = _make_client(team_root)
    sidecar = artifact_signing.sidecar_path(_skill_md(team_root, "olivia"))
    before = sidecar.read_bytes()

    # Reachable to the operator — otherwise the refusal below proves nothing.
    assert any(
        it["name"] == "reporter"
        for it in client.get("/api/trust/gated?include_loaded=1", headers=_OPERATOR).json()[
            "gated"
        ]
    )

    resp = client.post(
        "/api/trust/approve", headers=_VIEWER, json={"agent_id": "olivia", "name": "reporter"}
    )

    assert resp.status_code == 403
    assert sidecar.read_bytes() == before


def test_h1_an_unauthenticated_caller_cannot_sign(tmp_path: Path) -> None:
    _operator_seed(tmp_path)
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)

    for headers in ({}, {"Authorization": "Bearer guessed-token"}):
        resp = client.post(
            "/api/trust/approve", headers=headers, json={"agent_id": "olivia", "name": "reporter"}
        )
        assert resp.status_code == 401
    assert _signatures_under(team_root) == []


def test_h1_a_viewer_cannot_revoke_a_signature(tmp_path: Path) -> None:
    """Withdrawing trust is a trust mutation too — it gates a live capability."""
    _operator_seed(tmp_path)
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="personal", sign=True)
    client = _make_client(team_root)
    sidecar = artifact_signing.sidecar_path(_skill_md(team_root, "olivia"))
    before = sidecar.read_bytes()

    resp = client.post(
        "/api/trust/disapprove", headers=_VIEWER, json={"agent_id": "olivia", "name": "reporter"}
    )

    assert resp.status_code == 403
    assert sidecar.read_bytes() == before


# ─────────────────────────────────────────────────────────────────────────────
# H2 — Reachable from no tool registry and no agent path (REQ-321)
# ─────────────────────────────────────────────────────────────────────────────

#: What an agent-reachable signing primitive would have to name to be useful.
_SIGNING_SEAM = ("sign_capability", "capability_signing", "write_signature_with_signer")

#: The operator key's own resolution seam. A tool that could reach THIS could
#: sign with the operator's authority without touching the seam names above.
_OPERATOR_KEY_SEAM = ("OperatorKey", "default_operator_key_path", "operator_key_dir")


def _sources(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def test_h2_no_builtin_agent_tool_names_the_signing_seam() -> None:
    """The agent's own capability tools cannot import their way to signing.

    ``create_skill``/``update_skill`` DO sign, with the agent's own DID
    (SPEC-033) — that is self-attestation and the loader still gates it. The
    operator seam is a different function reached through a different key, and
    no tool may name either.
    """
    offenders = [
        (path.name, token)
        for path in _sources(_ARCAGENT_SRC / "builtins")
        for token in (*_SIGNING_SEAM, *_OPERATOR_KEY_SEAM)
        if token in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "An agent-facing builtin names the operator signing/key seam — an agent "
        f"that can authorize its own code (REQ-321): {offenders}"
    )


def test_h2_no_registered_tool_is_a_signing_tool(tmp_path: Path) -> None:
    """Checked against the real inventory, not a hand-listed set of names.

    A new tool added to the builtins tree shows up here automatically, so this
    fails the day someone registers ``sign_capability`` as a callable tool.
    """
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="personal", sign=True)
    client = _make_client(team_root)

    rows = client.get("/api/trust/gated?include_loaded=1", headers=_OPERATOR).json()["gated"]
    tools = {row["name"] for row in rows if row["kind"] == "tool"}

    assert tools, "inventory returned no tools — this check would pass vacuously"
    assert not [name for name in tools if "sign" in name or "trust" in name], (
        f"a registered tool name looks like a signing primitive: {sorted(tools)}"
    )


def test_h2_the_signing_seam_is_called_from_exactly_one_arcui_module() -> None:
    """One caller, and it is the operator-authenticated HTTP route module.

    Fails if any other arcui module — a websocket handler, a chat path, a
    helper — starts calling ``arcagent.sign_capability``.
    """
    callers = {
        path.relative_to(_ARCUI_SRC).as_posix()
        for path in _sources(_ARCUI_SRC)
        if "sign_capability" in path.read_text(encoding="utf-8")
    }
    assert callers == {"routes/trust.py"}, f"unexpected signing callers in arcui: {callers}"


def test_h2_the_chat_websocket_module_cannot_reach_the_trust_surface() -> None:
    """``/ws/chat`` is the agent's own path into arcui. It imports no trust.

    An AST walk over the imports rather than a substring scan, so a module that
    merely mentions the word "trust" in prose is not a false positive and one
    that really imports the route module is not missed.
    """
    tree = ast.parse((_ARCUI_SRC / "routes" / "chat_ws.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)

    forbidden = {name for name in imported if "trust" in name.split(".")[-1] or "sign" in name}
    assert not forbidden, f"chat_ws imports a trust/signing name: {sorted(forbidden)}"


def test_h2_no_websocket_route_serves_a_trust_handler() -> None:
    """The whole app: every trust endpoint is an HTTP route, never a socket.

    Built from the REAL application so a future registration mistake — mounting
    ``approve`` on a WebSocketRoute, or exposing it under another prefix — is
    caught here rather than in review.
    """
    from arcui.server import create_app

    app = create_app()
    trust_handlers = {route.endpoint for route in trust_routes if isinstance(route, Route)}
    assert trust_handlers, "no trust handlers found — this check would pass vacuously"

    def walk(routes: list[Any]) -> list[Any]:
        flat: list[Any] = []
        for route in routes:
            flat.append(route)
            if isinstance(route, Mount) and hasattr(route, "routes"):
                flat.extend(walk(list(route.routes)))
        return flat

    all_routes = walk(list(app.routes))
    sockets = [r for r in all_routes if isinstance(r, WebSocketRoute)]
    assert sockets, "no websocket routes found — this check would pass vacuously"
    assert not [r for r in sockets if getattr(r, "endpoint", None) in trust_handlers]

    http = [
        r
        for r in all_routes
        if isinstance(r, Route) and getattr(r, "endpoint", None) in trust_handlers
    ]
    assert {r.path for r in http} == {
        "/api/trust/gated",
        "/api/trust/source",
        "/api/trust/approve",
        "/api/trust/disapprove",
    }


# ─────────────────────────────────────────────────────────────────────────────
# H3 — The operator key never leaves the process
# ─────────────────────────────────────────────────────────────────────────────


def _secret_forms(seed: bytes) -> list[str]:
    """The encodings a leaked seed would plausibly arrive in."""
    return [
        seed.hex(),
        base64.b64encode(seed).decode("ascii"),
        base64.b64encode(seed).decode("ascii").rstrip("="),
        base64.b16encode(seed).decode("ascii"),
    ]


def test_h3_no_trust_response_contains_the_operator_private_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Every route, happy path and error path, searched for the real seed bytes.

    The operator's PUBLIC key is expected on disk (it is pinned as a capability
    verifier) and is not searched for. Only the private seed is — that is the
    thing whose disclosure would let anyone mint trust.
    """
    seed = _operator_seed(tmp_path)
    secrets = _secret_forms(seed)
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)

    caplog.set_level(logging.DEBUG)
    signed = client.post(
        "/api/trust/approve", headers=_OPERATOR, json={"agent_id": "olivia", "name": "reporter"}
    )
    # Guard: the seed is only in this process's hands if the signing really ran.
    # A 500 here would make every assertion below pass without proving anything.
    assert signed.status_code == 200, signed.text

    responses = [
        signed,
        client.get("/api/trust/gated", headers=_OPERATOR),
        client.get("/api/trust/gated?include_loaded=1", headers=_OPERATOR),
        client.get(
            "/api/trust/source",
            params={"agent_id": "olivia", "name": "reporter"},
            headers=_OPERATOR,
        ),
        client.post(
            "/api/trust/disapprove",
            headers=_OPERATOR,
            json={"agent_id": "olivia", "name": "reporter"},
        ),
        # Error paths leak more often than happy ones: an exception repr can
        # carry the argument that caused it.
        client.post(
            "/api/trust/approve", headers=_OPERATOR, json={"agent_id": "ghost", "name": "reporter"}
        ),
        client.post("/api/trust/approve", headers=_OPERATOR, content=b"not json"),
    ]

    for resp in responses:
        assert seed not in resp.content
        for secret in secrets:
            assert secret not in resp.text
    assert seed.hex() not in caplog.text
    for secret in secrets:
        assert secret not in caplog.text


def test_h3_a_signer_failure_names_the_error_type_not_the_key(tmp_path: Path) -> None:
    """No operator key at all: the 500 must describe the fault, not the path.

    An error string that echoed the key location is a map to the file for
    anyone who can reach the dashboard unauthenticated-adjacent surfaces.
    """
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)

    resp = client.post(
        "/api/trust/approve", headers=_OPERATOR, json={"agent_id": "olivia", "name": "reporter"}
    )

    assert resp.status_code == 500
    assert "operator.key" not in resp.text
    assert str(tmp_path) not in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# H4 — The operator key file cannot be fetched through any serving route
# ─────────────────────────────────────────────────────────────────────────────

#: Two independent ways the key could be served, because refusing one proves
#: nothing about the other:
#:
#: 1. DIRECT — a mount whose root is (or contains) the operator directory. No
#:    traversal needed; the escape already happened at mount time.
#: 2. TRAVERSAL — a correctly rooted mount that fails to reject ``..``.
#:
#: The traversal forms are percent-encoded because httpx collapses literal
#: ``..`` segments client-side, so only the encoded ones actually arrive at the
#: server — which is also the only form a real attacker would send.
_DIRECT = [
    "/assets/operator/operator.key",
    "/assets/operator.key",
    "/assets/arc/operator/operator.key",
    "/operator/operator.key",
    "/operator.key",
    "/.arc/operator/operator.key",
    "/api/system-config/operator",
]
_TRAVERSALS = [
    *_DIRECT,
    "/assets/%2e%2e%2f%2e%2e%2f%2e%2e%2f%2e%2e%2foperator%2foperator.key",
    "/assets/..%2f..%2f..%2f..%2foperator%2foperator.key",
    "/assets/%2e%2e/%2e%2e/%2e%2e/operator/operator.key",
    "/assets/....//....//operator/operator.key",
    "/api/system-config/%2e%2e%2f%2e%2e%2foperator%2foperator",
]


def test_h4_no_route_serves_the_operator_key_file(tmp_path: Path) -> None:
    """The real app, every shape, against a key that really exists.

    arcui mounts ``/assets`` unauthenticated for the SPA bundle, so this is the
    surface where a mis-rooted mount or a directory escape would hand the
    signing key to any browser that can reach the dashboard port.

    The key file on disk is the RAW 32-byte seed, so the response BYTES are
    searched, not just the decoded text — a binary body decoded with
    replacement characters would hide the seed from a text-only scan.
    """
    seed = _operator_seed(tmp_path)
    secrets = _secret_forms(seed)
    key_path = default_operator_key_path()
    assert key_path.read_bytes() == seed, "the key file is not the raw seed this test searches for"

    from arcui.server import create_app

    app = create_app()
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app.state.auth_config = auth
    client = TestClient(app)

    for path in _TRAVERSALS:
        for headers in ({}, _OPERATOR):
            resp = client.get(path, headers=headers)
            assert seed not in resp.content, f"{path} served the operator key bytes"
            for secret in secrets:
                assert secret not in resp.text, f"{path} leaked the operator seed"


def test_h4_the_whole_operator_directory_is_unreachable(tmp_path: Path) -> None:
    """Not just ``operator.key`` — the notary keystore lives there too.

    A deployment on vault-transit custody keeps its notary keystore under the
    same directory, so an escape that reached any file in it is the same
    incident as reaching the key itself.
    """
    _operator_seed(tmp_path)
    marker = default_operator_key_path().parent / "notary" / "operator.priv"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("NOTARY-KEYSTORE-MARKER", encoding="utf-8")

    from arcui.server import create_app

    app = create_app()
    app.state.auth_config = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    client = TestClient(app)

    for path in (
        "/assets/operator/notary/operator.priv",
        "/assets/notary/operator.priv",
        "/assets/%2e%2e%2f%2e%2e%2f%2e%2e%2foperator%2fnotary%2foperator.priv",
        "/assets/..%2f..%2f..%2foperator%2fnotary%2foperator.priv",
        "/operator/notary/operator.priv",
    ):
        resp = client.get(path, headers=_OPERATOR)
        assert b"NOTARY-KEYSTORE-MARKER" not in resp.content


# ─────────────────────────────────────────────────────────────────────────────
# H5 — The agent's own file tools cannot write the key dir or a foreign .arcsig
# ─────────────────────────────────────────────────────────────────────────────
#
# Placed in the arcui suite because it is the other half of the same claim these
# routes rest on: locking the HTTP surface is worth nothing if the agent can
# write the operator key directory itself. The tools driven here are the real
# capability functions through the real ``_runtime``, not a re-derived check.


@pytest.fixture()
def fenced_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """One agent fenced to its workspace, with WRITABLE targets outside it.

    Making every target genuinely writable by the test user first is what keeps
    the refusals below meaningful: on the ordinary single-user install the
    process owns ``~/.arc``, so a passing test that leaned on file permissions
    would still pass with the fence deleted.
    """
    from arcagent.builtins.capabilities import _runtime

    arc_home = tmp_path / "arc"
    operator_dir = arc_home / "operator"
    operator_dir.mkdir(parents=True, exist_ok=True)
    operator_key = operator_dir / "operator.key"
    operator_key.write_text("SENTINEL-OPERATOR-KEY", encoding="utf-8")

    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="personal", sign=True)
    _build_agent(team_root, "marcus", tier="personal", sign=True)

    monkeypatch.setenv("ARC_CONFIG_DIR", str(arc_home))
    _runtime.configure(
        workspace=team_root / "olivia" / "workspace", allowed_paths=None, tier="personal"
    )
    import os

    assert os.access(operator_dir, os.W_OK) and os.access(operator_key, os.W_OK)
    return {
        "operator_dir": operator_dir,
        "operator_key": operator_key,
        "foreign_sidecar": artifact_signing.sidecar_path(_skill_md(team_root, "marcus")),
    }


async def test_h5_a_tool_cannot_overwrite_the_operator_key(
    fenced_agent: dict[str, Path],
) -> None:
    from arcagent.builtins.capabilities.write import write
    from arcagent.core.errors import ToolError

    target = fenced_agent["operator_key"]
    with pytest.raises(ToolError) as caught:
        await write(str(target), "attacker-seed")

    assert caught.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"
    assert target.read_text(encoding="utf-8") == "SENTINEL-OPERATOR-KEY"


async def test_h5_a_tool_cannot_plant_a_new_key_in_the_operator_dir(
    fenced_agent: dict[str, Path],
) -> None:
    """Planting a key is as good as stealing one: it becomes a trusted signer."""
    from arcagent.builtins.capabilities.write import write
    from arcagent.core.errors import ToolError

    target = fenced_agent["operator_dir"] / "attacker.key"
    with pytest.raises(ToolError) as caught:
        await write(str(target), "attacker-seed")

    assert caught.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"
    assert not target.exists()


async def test_h5_a_tool_cannot_write_another_agents_signature(
    fenced_agent: dict[str, Path],
) -> None:
    """One agent forging trust for another is lateral movement across the fleet."""
    from arcagent.builtins.capabilities.write import write
    from arcagent.core.errors import ToolError

    target = fenced_agent["foreign_sidecar"]
    before = target.read_bytes()

    with pytest.raises(ToolError) as caught:
        await write(str(target), '{"signer_did": "did:arc:forged"}')

    assert caught.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"
    assert target.read_bytes() == before


async def test_h5_a_relative_traversal_is_refused_too(fenced_agent: dict[str, Path]) -> None:
    """A fence that compares path strings accepts this and rejects the absolute
    form; only resolving first and then testing ancestry rejects both."""
    from arcagent.builtins.capabilities.write import write
    from arcagent.core.errors import ToolError

    target = fenced_agent["operator_key"]
    relative = str(Path("..") / ".." / ".." / "arc" / "operator" / "operator.key")

    with pytest.raises(ToolError) as caught:
        await write(relative, "attacker-seed")

    assert caught.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"
    assert target.read_text(encoding="utf-8") == "SENTINEL-OPERATOR-KEY"
