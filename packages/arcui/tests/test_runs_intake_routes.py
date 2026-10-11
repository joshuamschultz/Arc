"""SPEC-088 T-1359: run intake routes ``POST /api/runs`` and ``GET /api/runs/{run_id}``.

Covers COMP-001 (RunIntakeRoute, new module ``arcui.routes.runs_intake``) against
REQ-588..595, REQ-631 and REQ-633, D-840, D-850 and D-851.

Contract this file pins for the builder (T-1360)
-------------------------------------------------

Module ``arcui.routes.runs_intake`` exports ``routes`` (a list of Starlette
routes): ``POST /api/runs`` and ``GET /api/runs/{run_id}``. The routes read
only these ``app.state`` attributes:

* ``task_store`` - the arcstore ``TaskStore`` (intake rows go through
  ``TaskStore.create_if_absent(task, request_digest=)``).
* ``roster_provider`` - zero-arg callable returning ``RosterEntry`` rows; the
  body ``agent`` is matched against ``agent_id`` (handle) or ``did``, and the
  agent folder is ``Path(entry.workspace_path)`` (``_agent_root`` semantics).
  "Accepts runs" is read from ``<agent folder>/arcagent.toml`` using the layout
  the tasks module actually loads: ``[modules.tasks]`` ``enabled`` (default
  true, as ``ModuleEntry``) and ``[modules.tasks.config]`` ``dispatch`` (a real
  boolean ``true``). Missing file, bad TOML, wrong types or a ``dispatch`` key
  in the wrong table all refuse with 409 (fail closed).
* ``callback_policy`` - COMP-013 ``CallbackPolicy`` or absent/None. Its
  ``check(url)`` returns the matching allowlist entry, or raises ``ValueError``
  (T-1368's refusal type must subclass ``ValueError``) when the URL is not
  allowed, which includes an empty allowlist. It is called before any row is
  created.
* ``callback_dispatcher`` - COMP-013 ``CallbackDispatcher`` or absent/None.
  It exists only when the COMP-019 work-events signer exists.

With ``callback_url`` set: dispatcher or policy absent -> 503
``{"error": "notifications_unavailable"}``; policy refuses -> 400
``{"error": "callback_not_allowed"}``. Without ``callback_url`` both may be
physically absent and intake still works (REQ-633).

Audit (REQ-631): every POST outcome writes exactly one mutation audit row with
``operation == "run.intake"`` (outcome ``applied`` for 202/200, ``denied`` for
4xx, ``error`` for 5xx, the ``arcui.acting`` convention). An app refused on
``GET /api/runs/{run_id}`` gets one ``denied`` row with
``operation == "run.read"``. Rows name the acting identity (``actor_did``,
``on_behalf_of``, ``via_app``).

Doubles: only the principal (``principal_factory`` binds a real
``RequestPrincipal``), the audit WORM spy, and the callback policy/dispatcher
seam (COMP-013 is built by T-1368/T-1370). The task store is the real
``TaskStore`` over the per-test in-memory backend.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcgateway.team_roster import RosterEntry
from arcstore.tasks import Task, TaskStore
from arctrust.users import OPERATOR, VIEWER
from packages.arcui.tests.principal_fixtures import (
    agent_did,
    build_principal_app,
    issue_principal,
    principal_rig,
    write_team,
)
from packages.arcui.tests.user_authority import user_store_factory
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.principals import Scope

OLIVIA = agent_did()
BOB = agent_did()
PERSON = "did:arc:default:user/alice"
RUNS = "/api/runs"
INTAKE_OP = "run.intake"
READ_OP = "run.read"

ACCEPTING = "[modules.tasks]\nenabled = true\n\n[modules.tasks.config]\ndispatch = true\n"

# Agent configs whose tasks dispatch is off: the agent would never run the row.
DISPATCH_OFF = {
    "no_tasks_module": "",
    "dispatch_default_off": "[modules.tasks]\nenabled = true\n",
    "dispatch_false": "[modules.tasks]\nenabled = true\n\n[modules.tasks.config]\ndispatch = false\n",
    "module_disabled": "[modules.tasks]\nenabled = false\n\n[modules.tasks.config]\ndispatch = true\n",
    # The tasks module reads dispatch from [modules.tasks.config]; here it is not set.
    "dispatch_in_wrong_table": "[modules.tasks]\nenabled = true\ndispatch = true\n",
}

# Agent configs that cannot be read as a definite "on": refuse (fail closed).
UNREADABLE = {
    "malformed_toml": "[modules.tasks\nenabled = = true\n",
    "dispatch_not_a_bool": '[modules.tasks]\nenabled = true\n\n[modules.tasks.config]\ndispatch = "yes"\n',
    "tasks_not_a_table": '[modules]\ntasks = "on"\n',
}


# --- fleet doubles (roster rows over real agent folders) -----------------------


def _agent_folder(root: Path, label: str, did: str, tasks_toml: str | None) -> Path:
    """``team/<label>_agent/`` with an ``arcagent.toml``; ``None`` writes no file."""
    folder = root / f"{label}_agent"
    (folder / "workspace").mkdir(parents=True, exist_ok=True)
    if tasks_toml is not None:
        (folder / "arcagent.toml").write_text(
            f'[agent]\nname = "{label}"\norg = "default"\ntype = "executor"\n\n'
            f'[identity]\ndid = "{did}"\n\n{tasks_toml}',
            encoding="utf-8",
        )
    return folder


def _entry(label: str, did: str, folder: Path) -> RosterEntry:
    return RosterEntry(
        agent_id=label,
        name=label,
        did=did,
        org="default",
        type="executor",
        workspace_path=str(folder),
        model=None,
        provider=None,
        online=True,
        display_name=label,
        color="#000000",
        role_label="",
        hidden=False,
    )


# --- the rig -------------------------------------------------------------------


class Rig:
    """A bare Starlette app carrying only the intake routes and their state."""

    def __init__(
        self, tmp_path: Path, principal_factory: Any, backend: Any, routes: list[Any]
    ) -> None:
        self.factory = principal_factory
        self.team = tmp_path / "team"
        self.entries: list[RosterEntry] = []
        self.app = Starlette(routes=routes)
        self.worm = principal_factory.install(self.app)
        self.store = TaskStore(backend)
        self.app.state.task_store = self.store
        self.app.state.roster_provider = lambda: list(self.entries)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.add_agent("olivia", OLIVIA, ACCEPTING)
        self.add_agent("bob", BOB, ACCEPTING)

    def add_agent(self, label: str, did: str, tasks_toml: str | None) -> str:
        self.entries.append(_entry(label, did, _agent_folder(self.team, label, did, tasks_toml)))
        return did

    def act_as(self, principal: Any) -> Any:
        self.factory.act_as(self.app, principal)
        return principal

    def as_app(self, scopes: dict[Scope, tuple[str, ...]], **kw: Any) -> Any:
        return self.act_as(self.factory.app(scopes, **kw))

    def post(
        self, body: Any, *, key: str | None = "key-1", headers: dict[str, str] | None = None
    ) -> httpx.Response:
        sent = dict(headers or {})
        if key is not None:
            sent["Idempotency-Key"] = key
        return self.client.post(RUNS, json=body, headers=sent)

    def post_raw(self, content: bytes, *, key: str = "key-1") -> httpx.Response:
        return self.client.post(
            RUNS,
            content=content,
            headers={"Idempotency-Key": key, "Content-Type": "application/json"},
        )

    def get(self, run_id: str) -> httpx.Response:
        return self.client.get(f"{RUNS}/{run_id}")

    def tasks(self) -> list[Task]:
        return asyncio.run(self.store.list())

    def row(self, run_id: str) -> Task | None:
        return asyncio.run(self.store.get(f"intake-{run_id}"))

    def patch_row(self, run_id: str, patch: dict[str, Any]) -> None:
        asyncio.run(self.store.update(f"intake-{run_id}", patch, actor_did="did:arc:test:runner"))

    def mark(self) -> int:
        return len(self.worm.written)

    def rows_since(self, mark: int) -> list[Any]:
        return list(self.worm.written[mark:])


@pytest.fixture
def rig(tmp_path: Path, principal_factory: Any, arcstore_backend: Any) -> Rig:
    module = importlib.import_module("arcui.routes.runs_intake")
    return Rig(tmp_path, principal_factory, arcstore_backend, list(module.routes))


def _starter(rig: Rig, **kw: Any) -> Any:
    """An app holding runs:start and runs:read for olivia."""
    return rig.as_app({Scope.RUNS_START: (OLIVIA,), Scope.RUNS_READ: (OLIVIA,)}, **kw)


def _one_row(rig: Rig, mark: int, *, outcome: str, operation: str = INTAKE_OP) -> Any:
    rows = rig.rows_since(mark)
    assert len(rows) == 1, [(r.operation, r.outcome, r.detail) for r in rows]
    (row,) = rows
    assert row.operation == operation
    assert row.outcome == outcome
    return row


def _assert_refused_without_record(rig: Rig, mark: int, outcome: str = "denied") -> Any:
    assert rig.tasks() == []
    return _one_row(rig, mark, outcome=outcome)


def _nested(depth: int) -> dict[str, Any]:
    """A JSON object ``depth`` objects deep (the outer object counts as one)."""
    value: dict[str, Any] = {"leaf": 1}
    for _ in range(depth - 1):
        value = {"n": value}
    return value


# ===============================================================================
# 202: the run request is recorded and accepted (REQ-588)
# ===============================================================================


class TestAccept:
    def test_post_runs_valid_request_returns_202_queued_with_links(self, rig: Rig) -> None:
        _starter(rig)

        resp = rig.post({"agent": "olivia", "task": "Summarize the Q3 report"})

        assert resp.status_code == 202, resp.text
        body = resp.json()
        run_id = body["run_id"]
        assert uuid.UUID(run_id).version == 5
        assert body["status"] == "queued"
        assert body.get("replayed") in (None, False)
        links = body["links"]
        assert links["run"] == f"{RUNS}/{run_id}"
        assert links["timeline"] == f"{RUNS}/{run_id}/timeline"
        assert "events" in links

    def test_post_runs_resolves_agent_by_did(self, rig: Rig) -> None:
        _starter(rig)

        resp = rig.post({"agent": OLIVIA, "task": "t"})

        assert resp.status_code == 202, resp.text
        row = rig.row(resp.json()["run_id"])
        assert row is not None
        assert row.owner_did == OLIVIA

    def test_post_runs_stores_intake_row_with_platform_metadata(self, rig: Rig) -> None:
        app = _starter(rig)
        task = "Investigate the shipment delay. " * 10  # longer than a title
        caller_meta = {"order": "SO-42", "nested": {"lines": [1, 2]}}

        resp = rig.post(
            {"agent": "olivia", "task": task, "metadata": caller_meta, "correlation_id": "SO-42.a"}
        )

        assert resp.status_code == 202, resp.text
        run_id = resp.json()["run_id"]
        row = rig.row(run_id)
        assert row is not None, "intake row must be stored as task id 'intake-<run_id>'"
        assert row.id == f"intake-{run_id}"
        assert row.owner_did == OLIVIA
        assert row.creator_did == app.did
        assert row.status == "todo"
        assert row.max_attempts == 1
        assert row.tags == ["intake"]
        assert row.title == task[:120]
        assert row.description == task
        assert row.metadata["intake"] == {
            "run_id": run_id,
            "run_purpose": "manual",
            "correlation_id": "SO-42.a",
            "callback_url": None,
            "on_behalf_of": None,
            "via_app": None,
        }
        assert row.metadata["app"] == caller_meta
        assert row.metadata.get("request_digest")
        assert len(rig.tasks()) == 1

    def test_post_runs_run_purpose_message_is_accepted_and_stored(self, rig: Rig) -> None:
        _starter(rig)

        resp = rig.post({"agent": "olivia", "task": "t", "run_purpose": "message"})

        assert resp.status_code == 202, resp.text
        row = rig.row(resp.json()["run_id"])
        assert row is not None
        assert row.metadata["intake"]["run_purpose"] == "message"

    def test_post_runs_caller_metadata_cannot_forge_platform_keys(self, rig: Rig) -> None:
        """D-850: caller metadata lives only under metadata.app."""
        _starter(rig)
        forged = {
            "intake": {"run_id": "forged", "on_behalf_of": "did:arc:default:user/ceo"},
            "workflow": {"run_id": "wf", "node_id": "approve"},
            "flow_run_id": "wf",
            "node_id": "approve",
            "request_digest": "0" * 64,
        }

        resp = rig.post({"agent": "olivia", "task": "t", "metadata": forged})

        assert resp.status_code == 202, resp.text
        run_id = resp.json()["run_id"]
        row = rig.row(run_id)
        assert row is not None
        assert row.metadata["intake"]["run_id"] == run_id
        assert row.metadata["intake"]["on_behalf_of"] is None
        for key in ("workflow", "flow_run_id", "node_id"):
            assert key not in row.metadata
        assert row.metadata["request_digest"] != "0" * 64
        assert row.metadata["app"] == forged

    def test_post_runs_app_alone_is_creator_and_audited(self, rig: Rig) -> None:
        app = _starter(rig)
        mark = rig.mark()

        resp = rig.post({"agent": "olivia", "task": "t"})

        assert resp.status_code == 202, resp.text
        row = _one_row(rig, mark, outcome="applied")
        assert row.actor_did == app.did
        assert row.on_behalf_of is None
        assert row.via_app is None
        assert resp.json()["run_id"] in f"{row.target} {row.detail}"

    def test_post_runs_app_for_person_attributes_to_person_and_names_app(self, rig: Rig) -> None:
        app = _starter(rig, on_behalf_of=PERSON, person_roles=(OPERATOR,))
        mark = rig.mark()

        resp = rig.post({"agent": "olivia", "task": "t"})

        assert resp.status_code == 202, resp.text
        stored = rig.row(resp.json()["run_id"])
        assert stored is not None
        assert stored.creator_did == PERSON
        assert stored.metadata["intake"]["on_behalf_of"] == PERSON
        assert stored.metadata["intake"]["via_app"] == app.did
        audit = _one_row(rig, mark, outcome="applied")
        assert audit.actor_did == PERSON
        assert audit.on_behalf_of == PERSON
        assert audit.via_app == app.did

    def test_post_runs_operator_session_starts_run_as_its_account(self, rig: Rig) -> None:
        person = rig.act_as(rig.factory.user((OPERATOR,)))

        resp = rig.post({"agent": "olivia", "task": "t"})

        assert resp.status_code == 202, resp.text
        row = rig.row(resp.json()["run_id"])
        assert row is not None
        assert row.creator_did == person.did

    def test_post_runs_viewer_session_is_refused_with_one_denial(self, rig: Rig) -> None:
        rig.act_as(rig.factory.user((VIEWER,)))
        mark = rig.mark()

        resp = rig.post({"agent": "olivia", "task": "t"})

        assert resp.status_code == 403
        _assert_refused_without_record(rig, mark)

    def test_run_id_is_per_principal_for_the_same_key(self, rig: Rig) -> None:
        """run_id = uuid5(ns, principal DID + key): two apps never share a run."""
        body = {"agent": "olivia", "task": "t"}
        _starter(rig)
        first = rig.post(body, key="shared-key")
        again = rig.post(body, key="shared-key")
        _starter(rig)
        other = rig.post(body, key="shared-key")

        assert first.status_code == 202, first.text
        assert other.status_code == 202, other.text
        assert again.json()["run_id"] == first.json()["run_id"]
        assert other.json()["run_id"] != first.json()["run_id"]
        assert uuid.UUID(other.json()["run_id"]).version == 5
        assert len(rig.tasks()) == 2

    def test_different_keys_from_one_principal_are_distinct_runs(self, rig: Rig) -> None:
        _starter(rig)

        a = rig.post({"agent": "olivia", "task": "t"}, key="k-a")
        b = rig.post({"agent": "olivia", "task": "t"}, key="k-b")

        assert a.status_code == b.status_code == 202
        assert a.json()["run_id"] != b.json()["run_id"]
        assert len(rig.tasks()) == 2

    def test_intake_works_with_notification_seams_physically_absent(self, rig: Rig) -> None:
        """REQ-633: no callback policy/dispatcher on app.state, no callback_url -> 202."""
        assert not hasattr(rig.app.state, "callback_policy")
        assert not hasattr(rig.app.state, "callback_dispatcher")
        _starter(rig)

        assert rig.post({"agent": "olivia", "task": "t"}).status_code == 202


# ===============================================================================
# 400 / 413: only the declared fields, values and sizes (REQ-589, REQ-590, D-851)
# ===============================================================================

BAD_BODIES: dict[str, Any] = {
    "extra_field": {"agent": "olivia", "task": "t", "priority": "high"},
    "extra_owner_did": {"agent": "olivia", "task": "t", "owner_did": BOB},
    "extra_creator_did": {"agent": "olivia", "task": "t", "creator_did": "did:arc:x"},
    "purpose_schedule": {"agent": "olivia", "task": "t", "run_purpose": "schedule"},
    "purpose_pulse": {"agent": "olivia", "task": "t", "run_purpose": "pulse"},
    "purpose_workflow": {"agent": "olivia", "task": "t", "run_purpose": "workflow"},
    "purpose_unknown": {"agent": "olivia", "task": "t", "run_purpose": "admin"},
    "missing_agent": {"task": "t"},
    "missing_task": {"agent": "olivia"},
    "metadata_list": {"agent": "olivia", "task": "t", "metadata": [1, 2]},
    "metadata_string": {"agent": "olivia", "task": "t", "metadata": "x"},
    "metadata_too_deep": {"agent": "olivia", "task": "t", "metadata": _nested(10)},
    "correlation_too_long": {"agent": "olivia", "task": "t", "correlation_id": "c" * 129},
    "correlation_empty": {"agent": "olivia", "task": "t", "correlation_id": ""},
    "correlation_space": {"agent": "olivia", "task": "t", "correlation_id": "a b"},
    "correlation_slash": {"agent": "olivia", "task": "t", "correlation_id": "a/b"},
    "correlation_newline": {"agent": "olivia", "task": "t", "correlation_id": "a\nb"},
    "body_is_list": [{"agent": "olivia", "task": "t"}],
}


class TestValidation:
    @pytest.mark.parametrize("case", sorted(BAD_BODIES))
    def test_post_runs_bad_body_is_400_with_no_record_and_one_denial(
        self, rig: Rig, case: str
    ) -> None:
        _starter(rig)
        mark = rig.mark()

        resp = rig.post(BAD_BODIES[case])

        assert resp.status_code == 400, resp.text
        _assert_refused_without_record(rig, mark)

    def test_post_runs_non_json_body_is_400(self, rig: Rig) -> None:
        _starter(rig)
        mark = rig.mark()

        resp = rig.post_raw(b"agent=olivia&task=t")

        assert resp.status_code == 400, resp.text
        _assert_refused_without_record(rig, mark)

    def test_post_runs_metadata_over_16_kib_is_413(self, rig: Rig) -> None:
        _starter(rig)
        mark = rig.mark()

        resp = rig.post({"agent": "olivia", "task": "t", "metadata": {"blob": "x" * 17 * 1024}})

        assert resp.status_code == 413, resp.text
        _assert_refused_without_record(rig, mark)

    def test_post_runs_metadata_under_limits_is_accepted(self, rig: Rig) -> None:
        _starter(rig)

        big = rig.post(
            {"agent": "olivia", "task": "t", "metadata": {"blob": "x" * 15 * 1024}}, key="k-big"
        )
        deep = rig.post({"agent": "olivia", "task": "t", "metadata": _nested(7)}, key="k-deep")

        assert big.status_code == 202, big.text
        assert deep.status_code == 202, deep.text

    def test_post_runs_correlation_id_full_charset_at_128_chars_is_accepted(
        self, rig: Rig
    ) -> None:
        _starter(rig)
        correlation = ("Az09._:-" * 16)[:128]

        resp = rig.post({"agent": "olivia", "task": "t", "correlation_id": correlation})

        assert resp.status_code == 202, resp.text
        row = rig.row(resp.json()["run_id"])
        assert row is not None
        assert row.metadata["intake"]["correlation_id"] == correlation

    @pytest.mark.parametrize(
        "key",
        [None, "", "k" * 129, "has space", "a/b", "a\tb", "ключ"],
        ids=["missing", "empty", "too_long", "space", "slash", "tab", "non_ascii"],
    )
    def test_post_runs_missing_or_malformed_idempotency_key_is_400(
        self, rig: Rig, key: str | None
    ) -> None:
        _starter(rig)
        mark = rig.mark()

        if key is not None and not key.isascii():
            resp = rig.client.post(
                RUNS,
                json={"agent": "olivia", "task": "t"},
                headers=[(b"idempotency-key", key.encode("utf-8"))],
            )
        else:
            resp = rig.post({"agent": "olivia", "task": "t"}, key=key)

        assert resp.status_code == 400, resp.text
        _assert_refused_without_record(rig, mark)

    def test_post_runs_idempotency_key_at_128_safe_chars_is_accepted(self, rig: Rig) -> None:
        _starter(rig)

        resp = rig.post({"agent": "olivia", "task": "t"}, key=("K9._:-" * 30)[:128])

        assert resp.status_code == 202, resp.text


# ===============================================================================
# Replays (REQ-593)
# ===============================================================================


class TestIdempotency:
    def test_replay_same_key_and_body_returns_200_same_run_and_no_second_row(
        self, rig: Rig
    ) -> None:
        _starter(rig)
        body = {"agent": "olivia", "task": "t", "metadata": {"a": 1}, "correlation_id": "c1"}
        first = rig.post(body)
        mark = rig.mark()

        replay = rig.post(body)

        assert first.status_code == 202, first.text
        assert replay.status_code == 200, replay.text
        assert replay.json()["run_id"] == first.json()["run_id"]
        assert replay.json()["replayed"] is True
        assert len(rig.tasks()) == 1
        _one_row(rig, mark, outcome="applied")

    def test_replay_is_insensitive_to_json_key_order_and_whitespace(self, rig: Rig) -> None:
        _starter(rig)
        first = rig.post_raw(b'{"agent":"olivia","task":"t","metadata":{"a":1,"b":2}}')

        replay = rig.post_raw(
            b'{ "metadata": {"b": 2, "a": 1},\n "task": "t", "agent": "olivia" }'
        )

        assert first.status_code == 202, first.text
        assert replay.status_code == 200, replay.text
        assert replay.json()["run_id"] == first.json()["run_id"]
        assert len(rig.tasks()) == 1

    @pytest.mark.parametrize(
        "changed",
        [
            {"task": "a different task"},
            {"metadata": {"a": 2}},
            {"correlation_id": "c2"},
            {"run_purpose": "message"},
            {"agent": "bob"},
        ],
        ids=["task", "metadata", "correlation", "purpose", "agent"],
    )
    def test_same_key_different_body_is_409_and_row_untouched(
        self, rig: Rig, changed: dict[str, Any]
    ) -> None:
        rig.as_app({Scope.RUNS_START: (OLIVIA, BOB), Scope.RUNS_READ: (OLIVIA, BOB)})
        body = {"agent": "olivia", "task": "t", "metadata": {"a": 1}, "correlation_id": "c1"}
        first = rig.post(body)
        before = rig.row(first.json()["run_id"])
        mark = rig.mark()

        conflict = rig.post({**body, **changed})

        assert first.status_code == 202, first.text
        assert conflict.status_code == 409, conflict.text
        assert rig.tasks() == [before]
        _one_row(rig, mark, outcome="denied")

    async def test_concurrent_identical_requests_create_exactly_one_row(self, rig: Rig) -> None:
        """Both requests reach the store write together; one creates, one replays."""
        barrier = asyncio.Barrier(2)
        real = rig.store

        class Interleaved:
            def __getattr__(self, name: str) -> Any:
                return getattr(real, name)

            async def create_if_absent(self, task: Task, *, request_digest: str) -> Any:
                await asyncio.wait_for(barrier.wait(), timeout=5)
                return await real.create_if_absent(task, request_digest=request_digest)

        rig.app.state.task_store = Interleaved()
        _starter(rig)
        body = {"agent": "olivia", "task": "t"}
        transport = httpx.ASGITransport(app=rig.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://arc") as client:
            responses = await asyncio.gather(
                client.post(RUNS, json=body, headers={"Idempotency-Key": "race"}),
                client.post(RUNS, json=body, headers={"Idempotency-Key": "race"}),
            )

        assert sorted(r.status_code for r in responses) == [200, 202], [r.text for r in responses]
        assert len({r.json()["run_id"] for r in responses}) == 1
        assert len(await real.list()) == 1


# ===============================================================================
# Refusals: unknown 404, out of scope 403, not accepting runs 409 (REQ-594)
# ===============================================================================


class TestRefusals:
    @pytest.mark.parametrize("agent", ["nobody", "did:arc:default:executor/nobody"])
    def test_unknown_agent_is_404_with_no_record_and_one_denial(
        self, rig: Rig, agent: str
    ) -> None:
        rig.as_app({Scope.RUNS_START: ("*",)})
        mark = rig.mark()

        resp = rig.post({"agent": agent, "task": "t"})

        assert resp.status_code == 404, resp.text
        _assert_refused_without_record(rig, mark)

    def test_app_without_runs_start_for_agent_is_403_with_one_denial(self, rig: Rig) -> None:
        app = rig.as_app({Scope.RUNS_READ: (OLIVIA,), Scope.TASKS_WRITE: (OLIVIA,)})
        mark = rig.mark()

        resp = rig.post({"agent": "olivia", "task": "t"})

        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "scope_denied"
        row = _assert_refused_without_record(rig, mark)
        assert row.actor_did == app.did

    def test_runs_start_for_another_agent_is_403(self, rig: Rig) -> None:
        rig.as_app({Scope.RUNS_START: (BOB,)})
        mark = rig.mark()

        resp = rig.post({"agent": "olivia", "task": "t"})

        assert resp.status_code == 403, resp.text
        _assert_refused_without_record(rig, mark)

    def test_denial_for_app_acting_for_person_names_both(self, rig: Rig) -> None:
        app = rig.as_app({}, on_behalf_of=PERSON, person_roles=(OPERATOR,))
        mark = rig.mark()

        resp = rig.post({"agent": "olivia", "task": "t"})

        assert resp.status_code == 403, resp.text
        row = _assert_refused_without_record(rig, mark)
        assert row.actor_did == PERSON
        assert row.on_behalf_of == PERSON
        assert row.via_app == app.did

    def test_out_of_scope_app_gets_403_not_409_for_dispatch_off_agent(self, rig: Rig) -> None:
        """Scope is checked first: an app never learns another agent's config."""
        off = rig.add_agent("quiet", agent_did(), DISPATCH_OFF["dispatch_default_off"])
        rig.as_app({Scope.RUNS_START: (OLIVIA,)})
        mark = rig.mark()

        resp = rig.post({"agent": "quiet", "task": "t"})

        assert off != OLIVIA
        assert resp.status_code == 403, resp.text
        _assert_refused_without_record(rig, mark)

    @pytest.mark.parametrize("case", sorted(DISPATCH_OFF))
    def test_agent_with_dispatch_off_is_409_with_no_record(self, rig: Rig, case: str) -> None:
        did = rig.add_agent("quiet", agent_did(), DISPATCH_OFF[case])
        rig.as_app({Scope.RUNS_START: (did,)})
        mark = rig.mark()

        resp = rig.post({"agent": "quiet", "task": "t"})

        assert resp.status_code == 409, resp.text
        _assert_refused_without_record(rig, mark)

    @pytest.mark.parametrize("case", sorted(UNREADABLE))
    def test_agent_with_unreadable_config_is_409_fail_closed(self, rig: Rig, case: str) -> None:
        did = rig.add_agent("broken", agent_did(), UNREADABLE[case])
        rig.as_app({Scope.RUNS_START: (did,)})
        mark = rig.mark()

        resp = rig.post({"agent": "broken", "task": "t"})

        assert resp.status_code == 409, resp.text
        _assert_refused_without_record(rig, mark)

    def test_agent_with_missing_config_file_is_409(self, rig: Rig) -> None:
        did = rig.add_agent("ghost", agent_did(), None)
        rig.as_app({Scope.RUNS_START: (did,)})
        mark = rig.mark()

        resp = rig.post({"agent": "ghost", "task": "t"})

        assert resp.status_code == 409, resp.text
        _assert_refused_without_record(rig, mark)

    def test_tasks_module_enabled_by_default_with_dispatch_on_is_accepted(self, rig: Rig) -> None:
        """``[modules.tasks]`` without ``enabled`` loads (ModuleEntry default true)."""
        did = rig.add_agent(
            "implicit", agent_did(), "[modules.tasks]\n\n[modules.tasks.config]\ndispatch = true\n"
        )
        rig.as_app({Scope.RUNS_START: (did,)})

        assert rig.post({"agent": "implicit", "task": "t"}).status_code == 202

    def test_operator_session_is_also_refused_for_dispatch_off_agent(self, rig: Rig) -> None:
        rig.add_agent("quiet", agent_did(), DISPATCH_OFF["dispatch_false"])
        rig.act_as(rig.factory.user((OPERATOR,)))
        mark = rig.mark()

        resp = rig.post({"agent": "quiet", "task": "t"})

        assert resp.status_code == 409, resp.text
        _assert_refused_without_record(rig, mark)

    def test_config_change_takes_effect_on_the_next_request(self, rig: Rig) -> None:
        """No cache: an operator turning dispatch off stops intake at once."""
        _starter(rig)
        assert rig.post({"agent": "olivia", "task": "t"}, key="k1").status_code == 202
        toml = Path(rig.entries[0].workspace_path) / "arcagent.toml"
        toml.write_text(toml.read_text().replace("dispatch = true", "dispatch = false"))

        assert rig.post({"agent": "olivia", "task": "t"}, key="k2").status_code == 409


# ===============================================================================
# callback_url: allowlist and notifications availability (COMP-013, REQ-633)
# ===============================================================================


class StubPolicy:
    """COMP-013 CallbackPolicy seam: records each check and the row count at that time."""

    def __init__(self, rig: Rig, *, allow: bool) -> None:
        self._rig = rig
        self._allow = allow
        self.checked: list[tuple[str, int]] = []

    def check(self, url: str) -> dict[str, Any]:
        self.checked.append((url, len(self._rig.tasks())))
        if not self._allow:
            raise ValueError("origin not allowlisted")
        return {"origin": "https://hooks.example.com", "allow_private": False}


CALLBACK = "https://hooks.example.com/arc/runs"


class TestCallback:
    def test_callback_with_empty_allowlist_is_400_callback_not_allowed(self, rig: Rig) -> None:
        policy = StubPolicy(rig, allow=False)
        rig.app.state.callback_policy = policy
        rig.app.state.callback_dispatcher = object()
        _starter(rig)
        mark = rig.mark()

        resp = rig.post({"agent": "olivia", "task": "t", "callback_url": CALLBACK})

        assert resp.status_code == 400, resp.text
        assert resp.json()["error"] == "callback_not_allowed"
        assert [url for url, _ in policy.checked] == [CALLBACK]
        _assert_refused_without_record(rig, mark)

    def test_callback_with_dispatcher_absent_is_503_and_nothing_created(self, rig: Rig) -> None:
        rig.app.state.callback_policy = StubPolicy(rig, allow=True)
        _starter(rig)
        mark = rig.mark()

        resp = rig.post({"agent": "olivia", "task": "t", "callback_url": CALLBACK})

        assert resp.status_code == 503, resp.text
        assert resp.json()["error"] == "notifications_unavailable"
        _assert_refused_without_record(rig, mark, outcome="error")

    def test_callback_with_dispatcher_none_is_503(self, rig: Rig) -> None:
        rig.app.state.callback_policy = StubPolicy(rig, allow=True)
        rig.app.state.callback_dispatcher = None
        _starter(rig)
        mark = rig.mark()

        resp = rig.post({"agent": "olivia", "task": "t", "callback_url": CALLBACK})

        assert resp.status_code == 503, resp.text
        assert resp.json()["error"] == "notifications_unavailable"
        _assert_refused_without_record(rig, mark, outcome="error")

    def test_callback_with_policy_absent_is_503(self, rig: Rig) -> None:
        rig.app.state.callback_dispatcher = object()
        _starter(rig)
        mark = rig.mark()

        resp = rig.post({"agent": "olivia", "task": "t", "callback_url": CALLBACK})

        assert resp.status_code == 503, resp.text
        assert resp.json()["error"] == "notifications_unavailable"
        _assert_refused_without_record(rig, mark, outcome="error")

    def test_allowed_callback_is_checked_before_the_row_and_stored(self, rig: Rig) -> None:
        policy = StubPolicy(rig, allow=True)
        rig.app.state.callback_policy = policy
        rig.app.state.callback_dispatcher = object()
        _starter(rig)

        resp = rig.post({"agent": "olivia", "task": "t", "callback_url": CALLBACK})

        assert resp.status_code == 202, resp.text
        assert policy.checked == [(CALLBACK, 0)]
        row = rig.row(resp.json()["run_id"])
        assert row is not None
        assert row.metadata["intake"]["callback_url"] == CALLBACK


# ===============================================================================
# GET /api/runs/{run_id} (REQ-595)
# ===============================================================================

PROJECTION_KEYS = {
    "run_id",
    "agent_did",
    "status",
    "correlation_id",
    "run_purpose",
    "metadata",
    "result",
    "error",
    "created_at",
    "started_at",
    "completed_at",
}


def _start(rig: Rig, **body: Any) -> str:
    """Start one run on olivia as an operator session; return its run_id."""
    rig.act_as(rig.factory.user((OPERATOR,)))
    resp = rig.post({"agent": "olivia", "task": "t", **body}, key=uuid.uuid4().hex)
    assert resp.status_code == 202, resp.text
    run_id: str = resp.json()["run_id"]
    return run_id


class TestGetRun:
    def test_get_run_with_runs_read_returns_projection(self, rig: Rig) -> None:
        run_id = _start(rig, metadata={"order": "SO-42"}, correlation_id="SO-42")
        rig.as_app({Scope.RUNS_READ: (OLIVIA,)})

        resp = rig.get(run_id)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == PROJECTION_KEYS
        assert body["run_id"] == run_id
        assert body["agent_did"] == OLIVIA
        assert body["status"] == "queued"
        assert body["correlation_id"] == "SO-42"
        assert body["run_purpose"] == "manual"
        assert body["metadata"] == {"order": "SO-42"}
        assert body["result"] is None
        assert body["error"] is None
        assert body["created_at"]
        assert body["started_at"] is None
        assert body["completed_at"] is None

    @pytest.mark.parametrize(
        ("patch", "expected"),
        [
            ({"status": "todo"}, "queued"),
            ({"status": "in_progress", "started_at": "2026-10-10T12:00:00+00:00"}, "running"),
            (
                {
                    "status": "done",
                    "output": {"text": "the answer is 42"},
                    "completed_at": "2026-10-10T12:01:00+00:00",
                },
                "done",
            ),
            ({"status": "failed", "last_error": "loop exploded"}, "failed"),
            ({"status": "failed", "resolution": "dead_letter"}, "failed"),
            ({"status": "failed", "resolution": "cancelled"}, "cancelled"),
        ],
        ids=["todo", "in_progress", "done", "failed", "failed_other", "cancelled"],
    )
    def test_get_run_maps_task_status(
        self, rig: Rig, patch: dict[str, Any], expected: str
    ) -> None:
        run_id = _start(rig)
        rig.patch_row(run_id, patch)

        resp = rig.get(run_id)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == expected
        if expected == "done":
            assert "the answer is 42" in json.dumps(body["result"])
            assert body["completed_at"] == patch["completed_at"]
        if expected == "running":
            assert body["started_at"] == patch["started_at"]
        if "last_error" in patch:
            assert "loop exploded" in str(body["error"])

    def test_get_run_never_exposes_platform_metadata(self, rig: Rig) -> None:
        run_id = _start(rig, metadata={"k": "v"})

        body = rig.get(run_id).json()

        assert body["metadata"] == {"k": "v"}
        assert "request_digest" not in json.dumps(body)

    @pytest.mark.parametrize(
        "scopes",
        [
            {Scope.RUNS_START: (OLIVIA,)},
            {Scope.RUNS_READ: (BOB,)},
            {},
        ],
        ids=["start_only", "read_other_agent", "no_scopes"],
    )
    def test_get_run_by_principal_without_runs_read_for_agent_is_404(
        self, rig: Rig, scopes: dict[Scope, tuple[str, ...]]
    ) -> None:
        run_id = _start(rig)
        rig.as_app(scopes)
        mark = rig.mark()

        resp = rig.get(run_id)

        assert resp.status_code == 404, resp.text
        assert run_id not in resp.text
        _one_row(rig, mark, outcome="denied", operation=READ_OP)

    def test_get_run_404_does_not_reveal_whether_the_run_exists(self, rig: Rig) -> None:
        run_id = _start(rig)
        rig.as_app({Scope.RUNS_READ: (BOB,)})

        hidden = rig.get(run_id)
        missing = rig.get(str(uuid.uuid4()))

        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json()

    def test_get_unknown_run_is_404_for_operator(self, rig: Rig) -> None:
        rig.act_as(rig.factory.user((OPERATOR,)))

        assert rig.get(str(uuid.uuid4())).status_code == 404

    def test_get_run_for_plain_task_is_404(self, rig: Rig) -> None:
        """A task that is not an intake row is not a run, whatever its id."""
        rig.act_as(rig.factory.user((OPERATOR,)))
        run_id = str(uuid.uuid4())
        asyncio.run(
            rig.store.create(
                Task(id=f"intake-{run_id}", title="x", creator_did="did:arc:x", owner_did=OLIVIA)
            )
        )

        assert rig.get(run_id).status_code == 404

    @pytest.mark.parametrize("roles", [(VIEWER,), (OPERATOR,)], ids=["viewer", "operator"])
    def test_get_run_sessions_keep_reads(self, rig: Rig, roles: tuple[str, ...]) -> None:
        run_id = _start(rig)
        rig.act_as(rig.factory.user(roles))

        assert rig.get(run_id).status_code == 200


# ===============================================================================
# Full app: registration, APP_ROUTES and the still-refused run list
# ===============================================================================


class TestFullApp:
    """Real ``create_app`` + real ``AuthMiddleware`` + a real issued principal."""

    @pytest.fixture
    def full(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arcstore_backend: Any
    ) -> tuple[Any, Callable[..., Any]]:
        monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
        team = write_team(tmp_path / "team", {"olivia": OLIVIA})
        toml = team / "olivia_agent" / "arcagent.toml"
        toml.write_text(toml.read_text() + "\n" + ACCEPTING, encoding="utf-8")
        rig = principal_rig(tmp_path / "authority")
        built = build_principal_app(
            tmp_path,
            principal_store_factory=rig.factory,
            users=user_store_factory(tmp_path / "accounts"),
            team_root=team,
        )
        built.app.state.task_store = TaskStore(arcstore_backend)

        def issue(scopes: dict[Scope, tuple[str, ...]]) -> Any:
            return issue_principal(rig.open(), scopes=scopes)

        return built, issue

    def test_app_cannot_list_runs(self, full: tuple[Any, Callable[..., Any]]) -> None:
        built, issue = full
        app = issue({Scope.RUNS_START: (OLIVIA,), Scope.RUNS_READ: (OLIVIA,)})

        resp = TestClient(built.app, raise_server_exceptions=False).get(
            RUNS, headers=app.bearer_headers()
        )

        assert resp.status_code == 403
        assert resp.json()["error"] == "route_not_allowed"

    def test_app_starts_and_reads_run_through_the_real_stack(
        self, full: tuple[Any, Callable[..., Any]]
    ) -> None:
        built, issue = full
        app = issue({Scope.RUNS_START: (OLIVIA,), Scope.RUNS_READ: (OLIVIA,)})
        client = TestClient(built.app, raise_server_exceptions=False)

        started = client.post(
            RUNS,
            json={"agent": "olivia", "task": "t"},
            headers=app.bearer_headers(**{"Idempotency-Key": "full-1"}),
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]
        read = client.get(f"{RUNS}/{run_id}", headers=app.bearer_headers())

        assert read.status_code == 200, read.text
        assert read.json()["agent_did"] == OLIVIA
