"""Every workflow tool, against the real engine, in the order an agent uses them.

Why this file exists: each tool had unit tests against a recording double, all
green, while the live surface failed one call at a time — a crash on the shape
models send, a bundle root resolved one level too high, a run store method that
existed nowhere. A double cannot catch any of those, because the double is
written from the same assumption the code is.

So: real DefinitionStore on a real bundle root, real control plane, real
validator, real arcstore. The only thing not exercised here is the runner
actually dispatching (that needs a fleet); every authoring and read tool is.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from arctrust import AgentIdentity, OperatorKey


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The workflows module as an agent process has it — nothing injected."""
    from arcagent.modules.workflows import _runtime

    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
    key_path = tmp_path / "config" / "operator" / "operator.key"
    OperatorKey.generate().save(key_path)

    _runtime.reset()
    _runtime.configure(
        config={},
        workspace=tmp_path / "workspace",
        identity=AgentIdentity.generate(org="local", agent_type="agent"),
        operator_signer=OperatorKey.load(key_path, generate_if_absent=False).into_signer(),
    )
    yield _runtime.state()
    _runtime.reset()


def _ok(raw: str) -> dict[str, Any]:
    """A tool result, asserting it is not an error list."""
    result = json.loads(raw)
    assert "errors" not in result, result
    return result


def _version(result: dict[str, Any]) -> int:
    return int(result["definition"]["version"])


async def test_the_whole_authoring_surface_works_in_sequence(agent: Any) -> None:
    """Create, add files, add a node, edit it, wire a trigger and channel, read back."""
    from arcagent.modules.workflows.capabilities import (
        workflow_add_node,
        workflow_create,
        workflow_edit_node,
        workflow_inspect,
        workflow_list,
        workflow_put_files,
        workflow_remove_node,
        workflow_set_channel,
        workflow_set_trigger,
    )

    created = _ok(
        await workflow_create(
            workflow_id="client-update",
            description="Client follow-up",
            owner="@sales",
            nodes=[
                {
                    "id": "review",
                    "kind": "agent",
                    "agent": "@sales",
                    "prompt": "prompts/review.md",
                    "output_schema": "schemas/clients.json",
                }
            ],
            files={
                "prompts/review.md": "List the clients needing follow-up.",
                "schemas/clients.json": '{"type": "object", "required": ["clients"]}',
            },
        )
    )
    version = _version(created)

    # The call that was rejected on every live attempt: an edit carrying NO
    # files, whose existing prompt and schema must still resolve.
    added = _ok(
        await workflow_add_node(
            workflow_id="client-update",
            expected_version=version,
            node={"id": "research", "kind": "agent", "agent": "@sales", "needs": ["review"]},
        )
    )
    version = _version(added)
    assert [n["id"] for n in added["definition"]["nodes"]] == ["review", "research"]

    # Naming a prompt and writing it is ONE call: a node referencing a file the
    # bundle does not have is refused, so splitting them can never succeed.
    edited = _ok(
        await workflow_edit_node(
            workflow_id="client-update",
            node_id="research",
            expected_version=version,
            updates={"prompt": "prompts/research.md"},
            files={"prompts/research.md": "Search once per client."},
        )
    )
    version = _version(edited)

    version = _version(
        _ok(
            await workflow_put_files(
                workflow_id="client-update",
                expected_version=version,
                files={"prompts/review.md": "List the clients needing follow-up, ranked."},
            )
        )
    )

    version = _version(
        _ok(
            await workflow_set_trigger(
                workflow_id="client-update",
                expected_version=version,
                trigger={"type": "cron", "expression": "0 9 * * MON"},
            )
        )
    )
    version = _version(
        _ok(
            await workflow_set_channel(
                workflow_id="client-update",
                expected_version=version,
                channel="channel://client-update",
            )
        )
    )

    inspected = _ok(await workflow_inspect(workflow_id="client-update"))
    assert inspected["definition"]["trigger"]["expression"] == "0 9 * * MON"
    assert inspected["definition"]["channel"] == "channel://client-update"

    listed = json.loads(await workflow_list())
    assert [w["definition"]["id"] for w in listed] == ["client-update"]

    removed = _ok(
        await workflow_remove_node(
            workflow_id="client-update", node_id="research", expected_version=version
        )
    )
    assert [n["id"] for n in removed["definition"]["nodes"]] == ["review"]


async def test_every_authoring_tool_survives_a_json_string_argument(agent: Any) -> None:
    """The shape models actually send, on every tool that takes a structure."""
    from arcagent.modules.workflows.capabilities import (
        workflow_add_node,
        workflow_create,
        workflow_edit_node,
        workflow_set_trigger,
    )

    created = _ok(
        await workflow_create(
            workflow_id="strings",
            owner="@sales",
            nodes=json.dumps([{"id": "one", "kind": "agent", "agent": "@sales"}]),
            files=json.dumps({"prompts/one.md": "Do the thing."}),
        )
    )
    version = _version(created)

    version = _version(
        _ok(
            await workflow_add_node(
                workflow_id="strings",
                expected_version=version,
                node=json.dumps({"id": "two", "kind": "agent", "agent": "@sales"}),
            )
        )
    )
    version = _version(
        _ok(
            await workflow_edit_node(
                workflow_id="strings",
                node_id="two",
                expected_version=version,
                updates=json.dumps({"needs": ["one"]}),
            )
        )
    )
    _ok(
        await workflow_set_trigger(
            workflow_id="strings",
            expected_version=version,
            trigger=json.dumps({"type": "manual"}),
        )
    )


async def test_a_stale_edit_names_the_version_to_use(agent: Any) -> None:
    """A refusal an agent can act on beats one it retries forever."""
    from arcagent.modules.workflows.capabilities import workflow_add_node, workflow_create

    _ok(
        await workflow_create(
            workflow_id="stale", owner="@sales", nodes=[{"id": "one", "kind": "agent"}]
        )
    )
    _ok(
        await workflow_add_node(
            workflow_id="stale", expected_version=1, node={"id": "two", "kind": "agent"}
        )
    )

    result = json.loads(
        await workflow_add_node(
            workflow_id="stale", expected_version=1, node={"id": "three", "kind": "agent"}
        )
    )

    assert result["errors"], result
    assert "2" in json.dumps(result["errors"]), "the refusal must name the current version"


async def test_the_signature_request_reaches_the_approvals_queue(agent: Any) -> None:
    """The agent's only path to a signature is asking for one."""
    from arcagent.modules.workflows.capabilities import (
        workflow_create,
        workflow_request_signature,
    )
    from arcstore.approvals import ApprovalStore
    from arcstore.backends.sqlite import SqliteBackend
    from arcstore.config import store_db_path

    _ok(
        await workflow_create(
            workflow_id="to-sign", owner="@sales", nodes=[{"id": "one", "kind": "agent"}]
        )
    )

    asked = json.loads(await workflow_request_signature(workflow_id="to-sign"))
    assert asked["status"] == "pending_operator_approval"

    backend = SqliteBackend(store_db_path(None))
    await backend.start()
    try:
        pending = await ApprovalStore(backend).list(status="pending")
    finally:
        await backend.stop()
    assert [a.arguments.get("workflow_id") for a in pending] == ["to-sign"]
    # Bound to the content hash, so approving cannot sign a later edit.
    assert pending[0].call_hash


async def test_the_run_history_tools_answer_without_a_hosted_runner(agent: Any) -> None:
    """An agent asks how a workflow has been going from a process with no runner."""
    from arcagent.modules.workflows.capabilities import workflow_create, workflow_runs

    _ok(
        await workflow_create(
            workflow_id="history", owner="@sales", nodes=[{"id": "one", "kind": "agent"}]
        )
    )

    runs = json.loads(await workflow_runs(workflow_id="history"))

    assert runs == []
