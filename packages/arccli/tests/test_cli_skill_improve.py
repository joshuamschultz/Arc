"""alpha-2 P8 — `arc skill improve` and the golden-set controls from the CLI.

::

    arc skill improve <skill> --agent <dir-or-name> --email EMAIL [--dry-run] [--yes] [--json]
    arc skill evals run <skill> --agent <dir-or-name> --email EMAIL [--json]
    arc skill evals regen <skill_path> --agent <dir-or-name> --email EMAIL [--yes]

Like ``arc agent promotion run`` the CLI never builds an offline improver: it logs
in to the serve process (ArcUI's account-authenticated HTTP API) and asks the
RUNNING agent, which owns the eval model, the sandbox, the gate and the audit
chain. ``improve`` always previews first (diff + gate verdict); it applies only
the previewed candidate, and only after confirmation (``--yes`` or a prompt).

The server is faked at the network boundary (``httpx.MockTransport``).
"""

from __future__ import annotations

import builtins
import getpass
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from arccli.commands.skill import skill_handler

TOKEN = "session-token-do-not-print-4410"
PASSWORD = "hidden-pass-9921"
EMAIL = "operator@example.com"
BASE = "/api/agents/olivia/skills/planner"
PREVIEW = {
    "status": "preview",
    "skill_name": "planner",
    "reason": "strict improvement: fixed failing case(s), no regression",
    "preview_id": "f" * 32,
    "candidate_id": "abc123def456",
    "diff": "--- current/SKILL.md\n+++ candidate/SKILL.md\n-old line\n+new line\n",
    "scores": {"accuracy": 4.0},
    "gate": {
        "accepted": True,
        "reason": "strict improvement: fixed failing case(s), no regression",
        "before_pass": 1,
        "after_pass": 2,
        "newly_passing": 1,
    },
}
APPLIED = {
    "status": "applied",
    "skill_name": "planner",
    "reason": "strict improvement",
    "candidate_id": "abc123def456",
}
RUN = {
    "status": "completed",
    "skill_name": "planner",
    "reason": "",
    "total": 2,
    "passed": 1,
    "failed": 1,
    "cases": [
        {"case_id": "evals/test_g.py::test_a", "passed": True, "detail": ""},
        {"case_id": "evals/test_g.py::test_b", "passed": False, "detail": "AssertionError"},
    ],
}
REGEN = {"status": "completed", "skill_name": "planner", "reason": "", "total": 4, "adopted": 3}


@dataclass
class _Server:
    role: str = "operator"
    replies: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/auth/login":
            return httpx.Response(200, json={"token": TOKEN, "role": self.role})
        if path == "/api/auth/logout":
            return httpx.Response(200, json={"ok": True})
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "unauthenticated"})
        key = path
        if path.endswith("/improve"):
            key = f"{path}?dry_run={request.url.params.get('dry_run', '1')}"
        status, body = self.replies.get(key, (404, {"error": "not found"}))
        return httpx.Response(status, json=body)

    def calls(self) -> list[str]:
        out = []
        for r in self.requests:
            if r.url.path.startswith("/api/auth"):
                out.append(r.url.path)
            elif r.url.path.endswith("/improve"):
                out.append(f"{r.url.path}?dry_run={r.url.params.get('dry_run')}")
            else:
                out.append(r.url.path)
        return out

    def body_of(self, suffix: str) -> dict[str, Any]:
        for r in self.requests:
            label = r.url.path + (
                f"?dry_run={r.url.params.get('dry_run')}"
                if r.url.path.endswith("/improve")
                else ""
            )
            if label.endswith(suffix):
                return json.loads(r.content.decode() or "{}")
        raise AssertionError(f"no request for {suffix}")


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> _Server:
    fake = _Server(
        replies={
            f"{BASE}/improve?dry_run=1": (200, dict(PREVIEW)),
            f"{BASE}/improve?dry_run=0": (200, dict(APPLIED)),
            f"{BASE}/evals/run": (200, dict(RUN)),
            f"{BASE}/evals/regen": (200, dict(REGEN)),
        }
    )
    original = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        kwargs.pop("transport", None)
        return original(transport=httpx.MockTransport(fake.respond), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": PASSWORD)
    return fake


def _decline(monkeypatch: pytest.MonkeyPatch) -> None:
    def eof(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr(builtins, "input", eof)


def _arc(*argv: str) -> int:
    try:
        skill_handler(list(argv))
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0


_AUTH = ("--agent", "olivia", "--email", EMAIL)


# -- arc skill improve ------------------------------------------------------------


def test_dry_run_shows_diff_and_gate_verdict_and_never_applies(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _arc("improve", "planner", *_AUTH, "--dry-run")

    out = capsys.readouterr().out
    assert code == 0
    assert "+new line" in out
    assert "accepted" in out.lower()
    assert "strict improvement" in out
    assert server.calls() == [
        "/api/auth/login",
        f"{BASE}/improve?dry_run=1",
        "/api/auth/logout",
    ]
    assert TOKEN not in out and PASSWORD not in out


def test_confirmed_improve_applies_exactly_the_previewed_candidate(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _arc("improve", "planner", *_AUTH, "--yes")

    assert code == 0
    assert server.calls() == [
        "/api/auth/login",
        f"{BASE}/improve?dry_run=1",
        f"{BASE}/improve?dry_run=0",
        "/api/auth/logout",
    ]
    assert server.body_of("?dry_run=0") == {"confirm": True, "preview_id": "f" * 32}
    assert "applied" in capsys.readouterr().out.lower()


def test_declined_confirmation_applies_nothing(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    _decline(monkeypatch)

    code = _arc("improve", "planner", *_AUTH)

    assert code != 0
    assert f"{BASE}/improve?dry_run=0" not in server.calls()
    assert server.calls()[-1] == "/api/auth/logout"


def test_a_preview_the_gate_rejects_is_never_applied(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    rejected = {**PREVIEW, "gate": {**PREVIEW["gate"], "accepted": False, "reason": "regression"}}
    server.replies[f"{BASE}/improve?dry_run=1"] = (200, rejected)

    code = _arc("improve", "planner", *_AUTH, "--yes")

    assert code != 0
    assert f"{BASE}/improve?dry_run=0" not in server.calls()
    assert "regression" in capsys.readouterr().err


def test_a_viewer_session_is_refused_before_any_control_request(server: _Server) -> None:
    server.role = "viewer"

    code = _arc("improve", "planner", *_AUTH, "--dry-run")

    assert code != 0
    assert server.calls() == ["/api/auth/login", "/api/auth/logout"]


def test_a_server_refusal_exits_nonzero_with_its_message(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    server.replies[f"{BASE}/improve?dry_run=1"] = (
        503,
        {"error": "agent is not running in this process"},
    )

    code = _arc("improve", "planner", *_AUTH, "--dry-run")

    assert code != 0
    assert "not running" in capsys.readouterr().err
    assert server.calls()[-1] == "/api/auth/logout"


def test_json_prints_the_preview(server: _Server, capsys: pytest.CaptureFixture[str]) -> None:
    code = _arc("improve", "planner", *_AUTH, "--dry-run", "--json")

    assert code == 0
    assert json.loads(capsys.readouterr().out)["preview_id"] == "f" * 32


def test_an_unsafe_skill_name_is_refused_before_any_request(server: _Server) -> None:
    code = _arc("improve", "../etc", *_AUTH, "--dry-run")

    assert code != 0
    assert server.requests == []


# -- arc skill evals run / regen ------------------------------------------------


def test_evals_run_prints_pass_fail_per_case_and_fails_on_a_failing_case(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _arc("evals", "run", "planner", *_AUTH)

    out = capsys.readouterr().out
    assert code != 0, "a failing golden case must fail the command"
    assert "PASS" in out and "test_a" in out
    assert "FAIL" in out and "test_b" in out
    assert f"{BASE}/evals/run" in server.calls()


def test_evals_run_accepts_a_skill_folder_path(server: _Server, tmp_path: Path) -> None:
    folder = tmp_path / "planner"
    folder.mkdir()

    _arc("evals", "run", str(folder), *_AUTH)

    assert f"{BASE}/evals/run" in server.calls()


def test_evals_run_needs_the_running_agent(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _arc("evals", "run", "planner")

    assert code != 0
    assert "--agent" in capsys.readouterr().err
    assert server.requests == []


def _skill_with_machine_suite(tmp_path: Path) -> Path:
    skill = tmp_path / "planner"
    evals = skill / "evals"
    evals.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: planner\n---\n", encoding="utf-8")
    body = '"""@generated golden anchors."""\n\ndef test_gen():\n    assert 1\n'
    (evals / "test_golden_generated.py").write_text(body, encoding="utf-8")
    import hashlib

    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    (evals / ".manifest.json").write_text(
        json.dumps({"files": {"test_golden_generated.py": {"sha256": digest}}}), encoding="utf-8"
    )
    return skill


def test_confirmed_regen_regenerates_on_the_running_agent(
    server: _Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    skill = _skill_with_machine_suite(tmp_path)

    code = _arc("evals", "regen", str(skill), *_AUTH, "--yes")

    out = capsys.readouterr().out
    assert code == 0
    assert "test_golden_generated.py" in out, "the preview diff is still shown first"
    assert server.body_of("/evals/regen") == {"confirm": True}
    assert "3" in out


def test_regen_without_the_running_agent_says_how(
    server: _Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    skill = _skill_with_machine_suite(tmp_path)

    code = _arc("evals", "regen", str(skill), "--yes")

    err = capsys.readouterr().err
    assert code != 0
    assert "--agent" in err
    assert server.requests == []
