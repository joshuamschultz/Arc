"""Architecture guard: arcui attributes actions to the acting identity, never a fixed operator.

SPEC-087 T-1291 (COMP-005, COMP-008; REQ-557, REQ-567, REQ-568, REQ-569).

Four invariants, each with a scanner that is tested directly against synthetic
source so a green run here means "nothing to flag", not "the scanner is blind":

(a) No ``did:arc:ui:operator`` / ``user://operator`` literal in
    ``arcui/routes/**`` or ``arcui/approval_notifications.py``. COMP-008 deleted
    every fixed operator attribution; a literal coming back is a person's
    decision silently recorded as "the operator" again. Routes that send mail
    name the transport sender through ``arcui.messaging.OPERATOR_MAIL_ENTITY``
    (COMP-009: the person and app ride in signed ``on_behalf_of`` / ``via``).
    Documented exceptions outside the scan scope are pinned where they live,
    so moving or deleting them forces this file to be revisited:

    * ``arcui/messaging.py`` ``OPERATOR_MAIL_ENTITY = "user://operator"`` is the
      operator transport entity (it signs mail and receives agent replies);
    * ``arcui/audit.py`` ``_UI_ACTOR_DID`` is the actor of last resort when no
      operator key exists to name one (uninitialised deployment).

(b) Only ``arcui/principals.py``, ``arcui/acting.py`` and
    ``arcui/routes/principals.py`` import ``arctrust.principals`` (directly,
    as a submodule, or through a name ``arctrust`` re-exports from it). The
    principal record module stays behind those three seams.

(c) Every ``APP_ROUTES`` row whose agent is NOT bound from the path by the
    middleware is served by a handler that binds the agent itself through one
    of the scope helpers (``require_scope``, ``control_refusal``,
    ``app_target_refusal``, ``admit_gate_decider``). Each row is resolved to its
    Starlette endpoint in the real ``create_app`` build and the handler source
    (plus the helpers it calls by name) is inspected. A row with no handler,
    or a handler that never binds the agent, lets an app act on any agent.

(d) A principal's bearer secret, its stored digest and every request
    signature never reach a log record (at DEBUG) or an audit row, on success
    and on refusal, bearer and signed.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import logging
import re
import types
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ARCUI_SRC = REPO_ROOT / "packages" / "arcui" / "src" / "arcui"
ARCTRUST_INIT = REPO_ROOT / "packages" / "arctrust" / "src" / "arctrust" / "__init__.py"

OPERATOR_DID_LITERAL = "did:arc:ui:operator"
OPERATOR_MAIL_LITERAL = "user://operator"
FIXED_OPERATOR_LITERALS = (OPERATOR_DID_LITERAL, OPERATOR_MAIL_LITERAL)

PRINCIPALS_MODULE = "arctrust.principals"
PRINCIPALS_IMPORTERS = frozenset(
    {
        ARCUI_SRC / "principals.py",
        ARCUI_SRC / "acting.py",
        ARCUI_SRC / "routes" / "principals.py",
    }
)

AGENT_BINDING_HELPERS = frozenset(
    {"require_scope", "control_refusal", "app_target_refusal", "admit_gate_decider"}
)
_HELPER_SEARCH_DEPTH = 4


# ---------------------------------------------------------------------------
# (a) fixed operator literals
# ---------------------------------------------------------------------------


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def fixed_operator_literals(source: str, filename: str) -> list[str]:
    """Every string constant naming the fixed operator."""
    tree = ast.parse(source, filename=filename)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if any(literal in node.value for literal in FIXED_OPERATOR_LITERALS):
            hits.append(f"{filename}:{node.lineno}: {node.value!r}")
    return hits


def _attribution_scope() -> list[Path]:
    files = sorted((ARCUI_SRC / "routes").rglob("*.py"))
    files.append(ARCUI_SRC / "approval_notifications.py")
    return files


class TestFixedOperatorLiteralScanner:
    """The scanner itself: it must flag what (a) forbids and pass what it allows."""

    @pytest.mark.parametrize(
        "snippet",
        [
            '_CREATOR = "did:arc:ui:operator"\n',
            'actor = request.state.did or "did:arc:ui:operator"\n',
            'store.approve(task_id, actor_did="did:arc:ui:operator")\n',
            'Entity(id="user://operator")\n',
            'MailSendRequest(sender="user://operator", on_behalf_of=p, via=v)\n',
        ],
    )
    def test_scanner_flags_fixed_operator_attribution(self, snippet: str) -> None:
        assert fixed_operator_literals(snippet, "synthetic.py"), snippet

    @pytest.mark.parametrize(
        "snippet",
        [
            "MailSendRequest(sender=OPERATOR_MAIL_ENTITY, on_behalf_of=p, via=v)\n",
            "actor = acting_identity(request).actor_did\n",
            'role_did = "did:arc:ui:viewer"\n',
        ],
    )
    def test_scanner_allows_the_named_constant_and_other_dids(self, snippet: str) -> None:
        assert fixed_operator_literals(snippet, "synthetic.py") == []


def test_no_fixed_operator_attribution_in_routes_or_approval_notifications() -> None:
    """COMP-008: routes record ``acting_identity(request)``, never a fixed operator."""
    files = _attribution_scope()
    assert (ARCUI_SRC / "routes" / "tasks.py") in files, "scan scope lost the routes tree"
    hits: list[str] = []
    for path in files:
        hits.extend(fixed_operator_literals(path.read_text(encoding="utf-8"), str(path)))
    assert not hits, (
        "Fixed operator attribution found. Record the acting identity "
        "(arcui.acting.acting_identity / decider) instead; SPEC-087 COMP-008.\n"
        + "\n".join(f"  {hit}" for hit in hits)
    )


def test_documented_operator_exceptions_still_live_where_documented() -> None:
    """The two exceptions outside the scan scope must not move or vanish unnoticed."""
    messaging = ast.parse((ARCUI_SRC / "messaging.py").read_text(encoding="utf-8"))
    entities = [
        node.value.value
        for node in ast.walk(messaging)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "OPERATOR_MAIL_ENTITY" for t in node.targets)
        and isinstance(node.value, ast.Constant)
    ]
    assert entities == [OPERATOR_MAIL_LITERAL], (
        "arcui/messaging.py `OPERATOR_MAIL_ENTITY` (operator transport entity) moved or changed. "
        "Update the documented exceptions in this test (and SPEC-087 COMP-009)."
    )

    audit = ast.parse((ARCUI_SRC / "audit.py").read_text(encoding="utf-8"))
    fallbacks = [
        node.value.value
        for node in ast.walk(audit)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_UI_ACTOR_DID" for t in node.targets)
        and isinstance(node.value, ast.Constant)
    ]
    assert fallbacks == [OPERATOR_DID_LITERAL], (
        "arcui/audit.py `_UI_ACTOR_DID` (no-operator-key fallback) moved or changed. "
        "Update the documented exceptions in this test."
    )


# ---------------------------------------------------------------------------
# (b) arctrust.principals stays behind three seams
# ---------------------------------------------------------------------------


def principals_reexports(arctrust_init_source: str) -> frozenset[str]:
    """Names the ``arctrust`` root re-exports from ``arctrust.principals``."""
    tree = ast.parse(arctrust_init_source)
    return frozenset(
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == PRINCIPALS_MODULE
        for alias in node.names
    )


def principals_imports(source: str, filename: str, reexports: frozenset[str]) -> list[str]:
    """Every way ``source`` reaches into ``arctrust.principals``."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        where = f"{filename}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.Import):
            hits.extend(
                f"{where}: import {a.name}"
                for a in node.names
                if a.name == PRINCIPALS_MODULE or a.name.startswith(PRINCIPALS_MODULE + ".")
            )
        elif isinstance(node, ast.ImportFrom) and node.module == PRINCIPALS_MODULE:
            hits.append(f"{where}: from {node.module} import {_names(node)}")
        elif isinstance(node, ast.ImportFrom) and node.module == "arctrust":
            leaked = [a.name for a in node.names if a.name == "principals" or a.name in reexports]
            if leaked:
                hits.append(f"{where}: from arctrust import {', '.join(leaked)}")
        elif isinstance(node, ast.Constant) and node.value == PRINCIPALS_MODULE:
            hits.append(f"{where}: dynamic import of {PRINCIPALS_MODULE!r}")
    return hits


def _names(node: ast.ImportFrom) -> str:
    return ", ".join(a.name for a in node.names)


class TestPrincipalsImportScanner:
    REEXPORTS = frozenset({"Scope", "PrincipalStore"})

    @pytest.mark.parametrize(
        "snippet",
        [
            "from arctrust.principals import Scope\n",
            "import arctrust.principals\n",
            "import arctrust.principals as p\n",
            "from arctrust import principals\n",
            "from arctrust import causal, Scope\n",
            "def f():\n    from arctrust.principals import PrincipalStore\n",
            'importlib.import_module("arctrust.principals")\n',
        ],
    )
    def test_scanner_flags_principals_imports(self, snippet: str) -> None:
        assert principals_imports(snippet, "synthetic.py", self.REEXPORTS), snippet

    @pytest.mark.parametrize(
        "snippet",
        [
            "from arctrust import causal\n",
            "from arctrust.principal_auth import parse_bearer\n",
            "from arcui.principals import require_scope\n",
            '"""Docs mention arctrust.principals.PrincipalStore."""\n',
        ],
    )
    def test_scanner_allows_other_imports(self, snippet: str) -> None:
        assert principals_imports(snippet, "synthetic.py", self.REEXPORTS) == []

    def test_reexport_list_is_read_from_the_real_arctrust_root(self) -> None:
        reexports = principals_reexports(ARCTRUST_INIT.read_text(encoding="utf-8"))
        assert "Scope" in reexports
        assert "PrincipalStore" in reexports


def test_arcui_imports_arctrust_principals_only_behind_its_seams() -> None:
    """Only principals.py, acting.py and routes/principals.py touch the principal module."""
    reexports = principals_reexports(ARCTRUST_INIT.read_text(encoding="utf-8"))
    sources = sorted(ARCUI_SRC.rglob("*.py"))
    assert set(PRINCIPALS_IMPORTERS) <= set(sources), "an allowed importer moved"
    hits: list[str] = []
    for path in sources:
        if path in PRINCIPALS_IMPORTERS:
            continue
        hits.extend(principals_imports(path.read_text(encoding="utf-8"), str(path), reexports))
    assert not hits, (
        "arcui reaches into arctrust.principals outside arcui/principals.py, arcui/acting.py "
        "and arcui/routes/principals.py. Import what you need from arcui.principals or "
        "arcui.acting instead (SPEC-087 COMP-005/COMP-006).\n"
        + "\n".join(f"  {hit}" for hit in hits)
    )


# ---------------------------------------------------------------------------
# (c) every handler-bound APP_ROUTES row binds its agent
# ---------------------------------------------------------------------------

_PARAM = re.compile(r"\{[A-Za-z_]+(?::[a-z]+)?\}")


def _called_names(func: Callable[..., Any]) -> set[str]:
    tree = ast.parse(inspect.cleandoc("\n" + inspect.getsource(func)))
    return {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (name := _call_name(node))
    }


def binds_agent(func: Callable[..., Any], depth: int = _HELPER_SEARCH_DEPTH) -> bool:
    """True when ``func`` (or a module-level helper it calls by name) calls a binding helper."""
    seen: set[int] = set()
    frontier: list[Callable[..., Any]] = [inspect.unwrap(func)]
    for _ in range(depth):
        next_frontier: list[Callable[..., Any]] = []
        for current in frontier:
            if id(current) in seen:
                continue
            seen.add(id(current))
            called = _called_names(current)
            if called & AGENT_BINDING_HELPERS:
                return True
            next_frontier.extend(_resolve_helpers(current, called))
        frontier = next_frontier
    return False


def _resolve_helpers(
    func: Callable[..., Any], names: Iterable[str]
) -> Iterator[Callable[..., Any]]:
    namespace = getattr(func, "__globals__", {})
    for name in names:
        target = namespace.get(name)
        if isinstance(target, types.FunctionType) and target.__module__.startswith("arcui"):
            yield target


def _endpoints_for(app: Any, method: str, pattern: re.Pattern[str]) -> list[Callable[..., Any]]:
    from starlette.routing import Mount, Route

    found: list[Callable[..., Any]] = []

    def walk(routes: list[Any], prefix: str) -> None:
        for route in routes:
            if isinstance(route, Mount):
                walk(list(getattr(route, "routes", []) or []), prefix + route.path)
            elif isinstance(route, Route) and method in (route.methods or set()):
                if pattern.fullmatch(_PARAM.sub("probe-1", prefix + route.path)):
                    found.append(route.endpoint)

    walk(list(app.routes), "")
    return found


class TestAgentBindingDetector:
    """The detector itself, against a synthetic arcui-named module on disk."""

    SOURCE = """
async def control_refusal(*args, **kwargs):
    return None


async def _helper_that_binds(request):
    return await control_refusal(request)


async def bound_directly(request):
    return await control_refusal(request)


async def bound_through_helper(request):
    return await _helper_that_binds(request)


async def unbound(request):
    return {"ok": True}


async def unbound_calls_unrelated_helper(request):
    return await unbound(request)
"""

    @pytest.fixture
    def module(self, tmp_path: Path) -> types.ModuleType:
        path = tmp_path / "arcui_synthetic_handlers.py"
        path.write_text(self.SOURCE, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("arcui_synthetic_handlers", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_detector_accepts_direct_and_helper_binding(self, module: types.ModuleType) -> None:
        assert binds_agent(module.bound_directly)
        assert binds_agent(module.bound_through_helper)

    def test_detector_flags_handlers_that_never_bind(self, module: types.ModuleType) -> None:
        assert not binds_agent(module.unbound)
        assert not binds_agent(module.unbound_calls_unrelated_helper)


@dataclass
class Harness:
    """A real ``create_app`` build with the principal seam, its authority and its roster."""

    built: Any
    rig: Any
    dids: dict[str, str]


def build_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **config: Any) -> Harness:
    """Hermetic build: no Postgres, no developer ``~/.arc`` (mirrors arcui's conftest)."""
    from arcstore.backends.memory import FakeBackend

    from packages.arcui.tests.principal_fixtures import (
        agent_did,
        build_principal_app,
        principal_rig,
        write_team,
    )
    from packages.arcui.tests.user_authority import user_store_factory

    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "arcstore"))
    backend = FakeBackend()
    monkeypatch.setattr("arcui.server.open_backend", lambda **_kw: backend)
    monkeypatch.setattr("arcui.observe.open_backend", lambda **_kw: backend)

    rig = principal_rig(tmp_path / "authority")
    dids = {"olivia": agent_did(), "bob": agent_did()}
    built = build_principal_app(
        tmp_path,
        principal_store_factory=rig.factory,
        users=user_store_factory(tmp_path / "accounts"),
        team_root=write_team(tmp_path / "team", dids),
        **config,
    )
    return Harness(built=built, rig=rig, dids=dids)


def test_every_handler_bound_app_route_binds_its_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-557/558: a row the middleware cannot agent-check is checked by its handler."""
    from arcui.principals import APP_ROUTES

    app = build_harness(tmp_path, monkeypatch, tier="personal").built.app

    handler_rows = [row for row in APP_ROUTES if row.agent_from_path is None]
    assert handler_rows, "no handler-bound rows: the table changed shape, revisit this test"

    problems: list[str] = []
    for row in handler_rows:
        endpoints = _endpoints_for(app, row.method, row.pattern)
        if not endpoints:
            problems.append(f"{row.method} {row.pattern.pattern}: no handler serves this row")
        for endpoint in endpoints:
            if not binds_agent(endpoint):
                where = f"{endpoint.__module__}.{endpoint.__qualname__}"
                problems.append(
                    f"{row.method} {row.pattern.pattern}: {where} never binds the agent"
                )
    assert not problems, (
        "APP_ROUTES rows whose handler does not bind the agent through "
        f"{sorted(AGENT_BINDING_HELPERS)}:\n" + "\n".join(f"  {p}" for p in problems)
    )


# ---------------------------------------------------------------------------
# (d) secrets and signatures never reach logs or audit rows
# ---------------------------------------------------------------------------


def leaked(needles: Iterable[str], haystacks: Iterable[str]) -> list[str]:
    """Each needle that appears in any haystack (needles shorter than 16 chars are refused)."""
    needles = list(needles)
    assert all(len(n) >= 16 for n in needles), "a short needle would match by accident"
    corpus = list(haystacks)
    return [n for n in needles if any(n in text for text in corpus)]


def _record_texts(records: Iterable[logging.LogRecord]) -> Iterator[str]:
    formatter = logging.Formatter()
    for record in records:
        yield record.getMessage()
        yield repr(record.__dict__)
        if record.exc_info:
            yield formatter.formatException(record.exc_info)


def _audit_texts(harness: Harness) -> Iterator[str]:
    for row in harness.built.worm.written:
        yield row.model_dump_json() if hasattr(row, "model_dump_json") else repr(row)
    for event in [*harness.rig.audit.events, *harness.rig.strict.events]:
        yield event.model_dump_json()


def test_leak_detector_finds_a_planted_secret() -> None:
    secret = "arcp_" + "a" * 32 + "." + "b" * 43
    record = logging.LogRecord("x", logging.DEBUG, __file__, 1, "auth %s", (secret,), None)
    assert leaked([secret], _record_texts([record])) == [secret]
    assert leaked([secret], ["nothing here"]) == []


def _bearer_requests(harness: Harness, client: Any) -> list[str]:
    """Success and refusals over a bearer credential; returns every credential value sent."""
    from arctrust.principal_auth import format_bearer, issue_secret
    from arctrust.principals import Scope

    from packages.arcui.tests.principal_fixtures import issue_principal

    issued = issue_principal(
        harness.rig.open(), scopes={Scope.INBOX_SEND: (harness.dids["olivia"],)}
    )
    wrong_secret, _ = issue_secret()
    forged = format_bearer(issued.record.id, wrong_secret)

    ok = client.post("/api/agents/olivia/inbox", json={"x": 1}, headers=issued.bearer_headers())
    assert ok.status_code not in (401, 403, 429), ok.text
    assert (
        client.post(
            "/api/agents/bob/inbox", json={"x": 1}, headers=issued.bearer_headers()
        ).status_code
        == 403
    )
    assert client.get("/api/approvals", headers=issued.bearer_headers()).status_code == 403
    assert (
        client.post(
            "/api/agents/olivia/inbox",
            json={"x": 1},
            headers={"Authorization": f"Bearer {forged}"},
        ).status_code
        == 401
    )
    _, secret = issued.bearer.split(".", 1)
    return [issued.bearer, secret, issued.record.secret_digest, forged, wrong_secret]


def _signed_requests(harness: Harness, client: Any) -> list[str]:
    """Success and refusals over signed requests; returns every signature and secret sent."""
    from arctrust.principal_auth import HEADER_SIGNATURE
    from arctrust.principals import Scope

    from packages.arcui.tests.principal_fixtures import issue_principal, signed_headers

    issued = issue_principal(
        harness.rig.open(), scopes={Scope.INBOX_SEND: (harness.dids["olivia"],)}
    )
    body = b'{"x":1}'
    path = "/api/agents/olivia/inbox"

    good = signed_headers(issued, method="POST", path=path, body=body)
    ok = client.post(path, content=body, headers=good)
    assert ok.status_code not in (401, 403, 429), ok.text

    tampered = signed_headers(issued, method="POST", path=path, body=body)
    sig = tampered[HEADER_SIGNATURE]
    tampered[HEADER_SIGNATURE] = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert client.post(path, content=body, headers=tampered).status_code == 401

    denied = signed_headers(issued, method="GET", path="/api/approvals")
    assert client.get("/api/approvals", headers=denied).status_code == 403

    assert client.post(path, content=body, headers=issued.bearer_headers()).status_code == 401

    _, secret = issued.bearer.split(".", 1)
    return [
        good[HEADER_SIGNATURE],
        tampered[HEADER_SIGNATURE],
        denied[HEADER_SIGNATURE],
        issued.bearer,
        secret,
        issued.record.secret_digest,
    ]


@pytest.mark.parametrize(
    ("tier_config", "drive"),
    [
        ({"tier": "personal"}, _bearer_requests),
        ({"tier": "enterprise", "require_signed_requests": True}, _signed_requests),
    ],
    ids=["bearer", "signed"],
)
def test_principal_secrets_and_signatures_never_reach_logs_or_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tier_config: dict[str, Any],
    drive: Callable[[Harness, Any], list[str]],
) -> None:
    from starlette.testclient import TestClient

    harness = build_harness(tmp_path, monkeypatch, **tier_config)
    caplog.set_level(logging.DEBUG)
    sent = drive(harness, TestClient(harness.built.app, raise_server_exceptions=False))

    assert caplog.records, "nothing was logged at DEBUG; the test is vacuous"
    assert harness.built.worm.denied(), "refusals wrote no audit rows; the test is vacuous"
    assert leaked(sent, _record_texts(caplog.records)) == [], "credential material in a log"
    assert leaked(sent, _audit_texts(harness)) == [], "credential material in an audit row"
