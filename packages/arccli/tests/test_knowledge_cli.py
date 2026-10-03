"""alpha-2 P26 — `arc knowledge`: the connected-source lifecycle from the CLI.

The CLI never builds an offline service: it logs in to the serve process and asks
the RUNNING agent (ArcUI's connected-data routes), exactly like the Knowledge tab.
The server is faked at the network boundary (``httpx.MockTransport``).
"""

from __future__ import annotations

import builtins
import getpass
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from arccli.commands.knowledge import knowledge_handler

TOKEN = "session-token-do-not-print-7731"
EMAIL = "operator@example.com"
K = "/api/agents/olivia/knowledge"
SRC = f"{K}/connected-sources/gmail-1"

SOURCE = {
    "connection_id": "gmail-1",
    "source_id": "gmail-1",
    "label": "Gmail work",
    "source_kind": "email",
    "status": "idle",
    "detail": "",
    "pages": 3,
    "bytes_processed": 10,
    "error_code": None,
    "last_synced_at": "2026-10-01T08:00:00+00:00",
    "documents_indexed": 42,
    "allowed_homes": ["document", "memory"],
}
RESOURCE = {
    "resource_id": "label:INBOX",
    "label": "Inbox",
    "resource_kind": "label",
    "selected": False,
    "detail": "",
}
PROPOSAL = {
    "source_id": "gmail-1",
    "homes": ["document"],
    "status": "pending",
    "approval_id": "appr-9",
    "detail": "",
    "allowed_homes": ["document", "memory"],
}


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
        key = f"{request.method} {path}"
        status, body = self.replies.get(key, (404, {"error": "not found"}))
        return httpx.Response(status, json=body)

    def calls(self) -> list[str]:
        return [
            f"{r.method} {r.url.path}"
            for r in self.requests
            if not r.url.path.startswith("/api/auth")
        ]

    def body_of(self, call: str) -> dict[str, Any]:
        for r in self.requests:
            if f"{r.method} {r.url.path}" == call:
                return json.loads(r.content.decode() or "{}")
        raise AssertionError(f"no request for {call}")


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> _Server:
    fake = _Server(
        replies={
            f"GET {K}/connected-sources": (200, {"items": [SOURCE]}),
            f"GET {K}/sync": (200, {"items": [SOURCE]}),
            f"GET {SRC}/resources": (200, {"items": [RESOURCE]}),
            f"POST {SRC}/resources": (200, {"items": [{**RESOURCE, "selected": True}]}),
            f"GET {SRC}/mapping": (200, {"item": PROPOSAL}),
            f"POST {SRC}/mapping": (200, {"item": PROPOSAL}),
            "POST /api/approvals/appr-9/approve": (200, {"status": "approved"}),
            f"POST {K}/sync/gmail-1/sync": (200, {"status": "scheduled"}),
            f"POST {K}/sync/gmail-1/reindex": (200, {"status": "scheduled"}),
            f"POST {K}/sync/gmail-1/relayout": (
                200,
                {"status": "relayout_done", "detail": "moved=3 repathed=3"},
            ),
            f"POST {K}/sync/gmail-1/revoke": (200, {"status": "revoked"}),
            f"POST {K}/connected-data/activate": (200, {"status": "activated", "detail": "ok"}),
        }
    )
    original = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        kwargs.pop("transport", None)
        return original(transport=httpx.MockTransport(fake.respond), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": "hidden-pass")
    return fake


def _arc(*argv: str) -> int:
    try:
        knowledge_handler(list(argv))
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0


_AUTH = ("--agent", "olivia", "--email", EMAIL)


def test_sources_minimum_args_lists_each_source(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _arc("sources", *_AUTH)

    out = capsys.readouterr().out
    assert code == 0
    assert "gmail-1" in out and "Gmail work" in out and "42" in out
    assert server.calls() == [f"GET {K}/connected-sources"]
    assert TOKEN not in out


def test_bare_call_prints_help_not_a_crash(capsys: pytest.CaptureFixture[str]) -> None:
    assert _arc() == 0
    assert "sources" in capsys.readouterr().out


def test_missing_agent_or_email_is_refused_before_any_login(server: _Server) -> None:
    assert _arc("sources") == 1
    assert server.requests == []


def test_resources_lists_selectable_containers(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _arc("resources", "gmail-1", *_AUTH) == 0
    assert "label:INBOX" in capsys.readouterr().out


def test_select_posts_the_resource_ids(server: _Server) -> None:
    assert _arc("select", "gmail-1", "label:INBOX", *_AUTH) == 0
    assert server.body_of(f"POST {SRC}/resources") == {"resource_ids": ["label:INBOX"]}


def test_select_without_any_resource_is_refused(server: _Server) -> None:
    assert _arc("select", "gmail-1", *_AUTH) == 1
    assert server.requests == []


def test_map_without_homes_shows_the_proposal(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _arc("map", "gmail-1", *_AUTH) == 0
    assert "appr-9" in capsys.readouterr().out
    assert server.calls() == [f"GET {SRC}/mapping"]


def test_map_with_homes_stages_a_mapping(server: _Server) -> None:
    assert _arc("map", "gmail-1", "--homes", "document,memory", *_AUTH) == 0
    assert server.body_of(f"POST {SRC}/mapping") == {"homes": ["document", "memory"]}


def test_approve_resolves_the_staged_mapping_approval(server: _Server) -> None:
    assert _arc("approve", "gmail-1", *_AUTH) == 0
    assert server.calls() == [f"GET {SRC}/mapping", "POST /api/approvals/appr-9/approve"]


def test_approve_with_nothing_staged_fails(server: _Server) -> None:
    server.replies[f"GET {SRC}/mapping"] = (200, {"item": None})
    assert _arc("approve", "gmail-1", *_AUTH) == 1
    assert "POST /api/approvals/appr-9/approve" not in server.calls()


@pytest.mark.parametrize("action", ["sync", "reindex", "relayout"])
def test_lifecycle_actions_post_to_the_sync_route(server: _Server, action: str) -> None:
    assert _arc(action, "gmail-1", *_AUTH) == 0
    assert server.calls() == [f"POST {K}/sync/gmail-1/{action}"]


def test_revoke_requires_confirmation(server: _Server, monkeypatch: pytest.MonkeyPatch) -> None:
    def eof(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr(builtins, "input", eof)
    assert _arc("revoke", "gmail-1", *_AUTH) == 1
    assert server.calls() == []


def test_revoke_with_yes_revokes(server: _Server) -> None:
    assert _arc("revoke", "gmail-1", "--yes", *_AUTH) == 0
    assert server.calls() == [f"POST {K}/sync/gmail-1/revoke"]


def test_status_reports_last_sync_items_and_errors(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    server.replies[f"GET {K}/sync"] = (
        200,
        {"items": [{**SOURCE, "error_code": "auth_required", "status": "error"}]},
    )
    assert _arc("status", *_AUTH) == 0
    out = capsys.readouterr().out
    assert "2026-10-01" in out and "42" in out and "auth_required" in out


def test_status_for_unknown_source_fails(server: _Server) -> None:
    assert _arc("status", "nope", *_AUTH) == 1


def test_activate_enables_the_module(server: _Server) -> None:
    assert _arc("activate", *_AUTH) == 0
    assert server.calls() == [f"POST {K}/connected-data/activate"]


def test_json_flag_emits_machine_output(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _arc("sources", "--json", *_AUTH) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["connection_id"] == "gmail-1"


def test_headless_add_map_sync_status_completes(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    """J1 F9: the whole journey, no browser."""
    for argv in (
        ("sources",),
        ("resources", "gmail-1"),
        ("select", "gmail-1", "label:INBOX"),
        ("map", "gmail-1", "--homes", "document"),
        ("approve", "gmail-1"),
        ("sync", "gmail-1"),
        ("status",),
    ):
        assert _arc(*argv, *_AUTH) == 0, argv
    assert "Gmail work" in capsys.readouterr().out
    assert len(server.calls()) == 8


MIGRATION = {
    "connection_id": "gmail-1",
    "status": "would_migrate",
    "detail": "",
    "documents": 42,
    "adopted": 40,
    "deduplicated": 2,
    "skipped": 0,
}


def test_migrate_dry_run_previews_and_changes_nothing(
    server: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    """P18-4: an operator sees what would move into the shared stores first."""
    server.replies[f"POST {K}/shared-migration"] = (200, {"dry_run": True, "items": [MIGRATION]})
    assert _arc("migrate", "--dry-run", *_AUTH) == 0
    assert server.calls() == [f"POST {K}/shared-migration"]
    assert server.body_of(f"POST {K}/shared-migration") == {"dry_run": True}
    out = capsys.readouterr().out
    assert "Dry run" in out and "would_migrate" in out and "40" in out


def test_migrate_requires_confirmation(server: _Server, monkeypatch: pytest.MonkeyPatch) -> None:
    def eof(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr(builtins, "input", eof)
    assert _arc("migrate", *_AUTH) == 1
    assert server.calls() == []


def test_migrate_with_yes_applies(server: _Server) -> None:
    server.replies[f"POST {K}/shared-migration"] = (
        200,
        {"dry_run": False, "items": [{**MIGRATION, "status": "migrated"}]},
    )
    assert _arc("migrate", "--yes", *_AUTH) == 0
    assert server.body_of(f"POST {K}/shared-migration") == {"dry_run": False}
