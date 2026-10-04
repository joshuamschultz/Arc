"""The sync worker writes only stores it can derive itself, and only under a live authority.

A request names a store; the worker works out the store's root on its own (from
the agent's own ``arcagent.toml``, or from the connection id) and refuses a
request whose root disagrees. A shared store is written only while the writer's
subscription stands, and dropped only once nobody reads it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arctrust.paths import connected_knowledge_dir

from arcagent.extension.knowledge_subscriptions import store_key
from arcagent.modules.connected_data.sync_worker.specs import StoreSpec
from arcagent.modules.connected_data.sync_worker.worker import (
    StoreRefusedError,
    StoreWriters,
)

_DID = "did:arc:test:agent"
_SOURCE = {"connection_id": "wiki", "source_kind": "confluence", "account_id": "acct"}


class _Host:
    """The main process's answers: which subscriptions exist."""

    def __init__(self, subscriptions: dict[tuple[str, str], str] | None = None) -> None:
        self.subscriptions = subscriptions or {}
        self.calls: list[str] = []

    async def call(self, op: str, args: dict[str, Any]) -> Any:
        self.calls.append(op)
        if op == "subscriptions.get":
            approval = self.subscriptions.get((args["agent_did"], args["connection_id"]))
            return None if approval is None else {"approval_id": approval}
        if op == "subscriptions.for_connection":
            return [
                {"agent_did": agent}
                for agent, connection in self.subscriptions
                if connection == args["connection_id"]
            ]
        raise AssertionError(f"unexpected host call {op}")


@pytest.fixture
def team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "arc"))
    agent_dir = tmp_path / "arc" / "team" / "agent_one"
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(
        f'[agent]\nname = "one"\nworkspace = "./workspace"\n\n[identity]\ndid = "{_DID}"\n',
        encoding="utf-8",
    )
    return agent_dir


def _own(agent_dir: Path, **overrides: Any) -> StoreSpec:
    values: dict[str, Any] = {
        "kind": "own",
        "agent_did": _DID,
        "root": str((agent_dir / "workspace").resolve()),
        "authority": "owner",
        "config_path": str(agent_dir / "arcagent.toml"),
    }
    return StoreSpec(**{**values, **overrides})


def _shared(authority: str = "subscriber", **overrides: Any) -> StoreSpec:
    values: dict[str, Any] = {
        "kind": "shared",
        "agent_did": _DID,
        "root": str((connected_knowledge_dir() / store_key("wiki")).resolve()),
        "authority": authority,
        "connection_id": "wiki",
        "approval_id": "approval-1",
    }
    return StoreSpec(**{**values, **overrides})


async def _refused(writers: StoreWriters, spec: StoreSpec, method: str, **args: Any) -> str:
    with pytest.raises(StoreRefusedError) as caught:
        await writers.call(spec, method, args, b"")
    return str(caught.value.wire["reason"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"root": "/etc"},
        {"agent_did": "did:arc:test:someone-else"},
        {"config_path": "/etc/passwd"},
        {"authority": "subscriber"},
    ],
    ids=["root-elsewhere", "not-this-agents-config", "not-an-agent-config", "wrong-authority"],
)
async def test_an_own_store_outside_the_allowed_set_is_refused(
    team: Path, overrides: dict[str, Any]
) -> None:
    host = _Host()
    writers = StoreWriters(host)  # type: ignore[arg-type]  # reason: the host's answers are scripted
    spec = _own(team, **overrides)
    method = "finish_sync" if spec.authority == "owner" else "reset_source"
    reason = await _refused(writers, spec, method, source=_SOURCE)
    assert reason.startswith(("store_root_not_allowed", "method_not_allowed"))
    assert not (team / "workspace" / "memory").exists(), "a refused write touched the store"


async def test_a_shared_store_named_by_another_root_is_refused(team: Path) -> None:
    writers = StoreWriters(_Host({(_DID, "wiki"): "approval-1"}))  # type: ignore[arg-type]  # reason: scripted host
    reason = await _refused(
        writers, _shared(root=str(team / "workspace")), "finish_sync", source=_SOURCE
    )
    assert reason == "store_root_not_allowed"


async def test_a_shared_write_without_a_live_subscription_is_refused(team: Path) -> None:
    host = _Host()  # the writer's subscription was revoked
    writers = StoreWriters(host)  # type: ignore[arg-type]  # reason: scripted host
    reason = await _refused(writers, _shared(), "finish_sync", source=_SOURCE)
    assert reason == "not_authorized"
    assert host.calls == ["subscriptions.get"]


async def test_a_subscription_under_another_approval_does_not_authorize(team: Path) -> None:
    writers = StoreWriters(_Host({(_DID, "wiki"): "approval-OLD"}))  # type: ignore[arg-type]  # reason: scripted host
    assert await _refused(writers, _shared(), "reset_source", source=_SOURCE) == "not_authorized"


async def test_a_subscriber_may_not_purge_the_shared_store(team: Path) -> None:
    writers = StoreWriters(_Host({(_DID, "wiki"): "approval-1"}))  # type: ignore[arg-type]  # reason: scripted host
    reason = await _refused(writers, _shared(), "purge_source", source=_SOURCE)
    assert reason == "method_not_allowed:subscriber:purge_source"


async def test_a_store_still_read_by_an_agent_is_not_dropped(team: Path) -> None:
    root = connected_knowledge_dir() / store_key("wiki")
    root.mkdir(parents=True)
    writers = StoreWriters(_Host({("did:arc:test:other", "wiki"): "a"}))  # type: ignore[arg-type]  # reason: scripted host
    assert await _refused(writers, _shared("orphan"), "drop_store") == "store_still_read"
    assert root.is_dir()

    await StoreWriters(_Host()).call(_shared("orphan"), "drop_store", {}, b"")  # type: ignore[arg-type]  # reason: scripted host
    assert not root.exists()


async def test_a_migration_may_only_adopt_the_writers_own_store(team: Path) -> None:
    host = _Host()
    writers = StoreWriters(host)  # type: ignore[arg-type]  # reason: scripted host

    async def approved(op: str, args: dict[str, Any]) -> Any:
        assert op == "approvals.list"
        return [{"id": "approval-1", "arguments": {"homes": "document"}}]

    host.call = approved  # type: ignore[method-assign]  # reason: the approval is on file
    donor = _own(team, agent_did="did:arc:test:someone-else").model_dump(mode="json")
    reason = await _refused(
        writers,
        _shared("migration"),
        "adopt_documents",
        source=_SOURCE,
        donor=donor,
        donor_source=_SOURCE,
        dry_run=True,
    )
    assert reason == "donor_not_the_writers_own_store"


async def test_an_unknown_method_is_refused(team: Path) -> None:
    writers = StoreWriters(_Host())  # type: ignore[arg-type]  # reason: scripted host
    assert await _refused(writers, _own(team), "exec") == "unknown_method:exec"
