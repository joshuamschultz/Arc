"""``arc task`` — human CLI surface over the mission-control task store (FR-14).

Mirrors the arcagent `tasks` module tools and arcui's operator mutation
routes, but calls the SAME arcstore ``TaskStore`` seam — there is no
parallel path. Every write (`create`/`edit`/`assign`/`complete`/`talk`) takes
a required ``--actor <ref>``, resolved to a DID via arcteam's
``EntityRegistry`` (the same resolve path ``arc team`` uses); the resolved
entity must carry the ``"operator"`` role or the command refuses — the CLI's
stand-in for arcui's bearer-token operator/viewer role, since a CLI
invocation has no session token to read a role from. ``list`` is read-only
and not gated (arcui parity: a viewer can read).

``edit`` is at-rest only (SDD §4/§6): it refuses with a nonzero exit when the
task is ``in_progress``, the same 409 arcui's `PATCH /api/tasks/{id}`
enforces. Steering an in-flight task is `talk`, which never touches
``TaskStore`` — it resolves the task's owner back to an arcteam entity and
sends a signed message to their inbox (mirrors ``arc team send``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arccli.commands._shared import dispatch
from arccli.commands._shared import err as _err
from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _out

if TYPE_CHECKING:
    from arcstore.backends.sqlite import SqliteBackend
    from arcstore.tasks import Task, TaskStore

_PRIORITIES = ("low", "medium", "high", "critical")


# ---------------------------------------------------------------------------
# Shared resolution helpers
# ---------------------------------------------------------------------------


def _get_root(args: argparse.Namespace) -> Path:
    """Resolve arcteam data root from args or TeamConfig default (mirrors team.py)."""
    root: str | None = getattr(args, "root", None)
    if root:
        return Path(root)
    from arcteam.config import TeamConfig

    return TeamConfig().root


def _resolve_dir(args: argparse.Namespace) -> Path:
    """Resolve arcstore data dir — same rule ``arc store`` uses (env > flag > default)."""
    from arcstore import resolve_data_dir

    return resolve_data_dir(getattr(args, "data_dir", None))


def _new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


def _audit_sink(data_dir: Path) -> Any:
    """Build the operator-signed WORM audit sink for mutable-plane writes.

    Module-level seam (not inlined) so tests can monkeypatch it to a
    recording fake and assert on real ``AuditEvent``s instead of re-deriving
    arctrust's emission logic.
    """
    from arcstore.ingest import WORM_ACTIVE_FILENAME
    from arctrust import WormSink

    from arccli.commands.operator import resolve_operator_signer

    worm_dir = data_dir / "worm"
    worm_dir.mkdir(parents=True, exist_ok=True)
    return WormSink(worm_dir / WORM_ACTIVE_FILENAME, resolve_operator_signer())


async def _open_store(data_dir: Path, *, mutable: bool) -> tuple[TaskStore, SqliteBackend]:
    """Open the shared arcstore TaskStore seam. ``mutable`` wires the audit sink.

    ``store_db_path`` is the single locator (ARCH-2) for the SAME tasks db the
    agents' ``tasks`` module and arcui read/write — NOT
    ``arccli.commands.store._db_path`` (``store/arcstore.db``), the unrelated
    ``arc store`` command's file — so a task created here is immediately
    visible to agents and the arcui kanban.
    """
    from arcstore import store_db_path
    from arcstore.backends.sqlite import SqliteBackend
    from arcstore.tasks import TaskStore

    backend = SqliteBackend(store_db_path(data_dir))
    await backend.start()
    sink = _audit_sink(data_dir) if mutable else None
    return TaskStore(backend, sink=sink), backend


async def _operator_entity(registry: Any, actor_ref: str) -> Any:
    """Resolve ``actor_ref`` and refuse (nonzero exit) unless it is an operator.

    Stand-in for arcui's bearer-token operator/viewer role check — the CLI has
    no session token, so the registered entity's ``roles`` list is the gate.
    """
    entity = await registry.get(actor_ref)
    if entity is None or "operator" not in entity.roles:
        _err(f"arc task: actor {actor_ref!r} is not a registered operator")
        sys.exit(1)
    return entity


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def _create(args: argparse.Namespace) -> None:
    root = _get_root(args)
    data_dir = _resolve_dir(args)

    async def _run() -> Task:
        from arcstore.tasks import Task
        from arcteam.registry import resolve

        from arccli.commands.team import _build_service, _shutdown

        _, registry, _, team_backend = await _build_service(root)
        try:
            entity = await _operator_entity(registry, args.actor)
            owner_did = await resolve(registry, args.owner) if args.owner else None
        finally:
            await _shutdown(team_backend)

        store, store_backend = await _open_store(data_dir, mutable=True)
        try:
            task = Task(
                id=_new_task_id(),
                title=args.title,
                description=args.description or "",
                priority=args.priority or "medium",
                owner_did=owner_did,
                creator_did=str(entity.did),
            )
            return await store.create(task)
        finally:
            await store_backend.stop()

    created = asyncio.run(_run())
    if getattr(args, "json", False):
        _out(json.dumps(created.model_dump(mode="json")))
    else:
        _out(f"Created task {created.id}: {created.title} [{created.status}]")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _list(args: argparse.Namespace) -> None:
    root = _get_root(args)
    data_dir = _resolve_dir(args)
    status: str | None = getattr(args, "status", None)
    owner_ref: str | None = getattr(args, "owner", None)
    scope: str | None = getattr(args, "scope", None)
    actor_ref: str | None = getattr(args, "actor", None)

    if scope == "mine" and not actor_ref:
        _err("arc task list: --scope mine requires --actor")
        sys.exit(2)

    async def _run() -> list[Task]:
        owner_did: str | None = None
        ref = actor_ref if scope == "mine" else owner_ref
        if ref is not None:
            from arcteam.registry import resolve

            from arccli.commands.team import _build_service, _shutdown

            _, registry, _, team_backend = await _build_service(root)
            try:
                owner_did = await resolve(registry, ref)
            finally:
                await _shutdown(team_backend)

        store, store_backend = await _open_store(data_dir, mutable=False)
        try:
            return await store.list(status=status, owner_did=owner_did)
        finally:
            await store_backend.stop()

    tasks = asyncio.run(_run())
    if getattr(args, "json", False):
        _out(json.dumps([t.model_dump(mode="json") for t in tasks]))
    elif not tasks:
        _out("No tasks.")
    else:
        rows = [[t.id, t.title, t.status, t.priority, t.owner_did or ""] for t in tasks]
        _print_table(["ID", "Title", "Status", "Priority", "Owner"], rows)


# ---------------------------------------------------------------------------
# edit — at-rest only
# ---------------------------------------------------------------------------


def _edit(args: argparse.Namespace) -> None:
    root = _get_root(args)
    data_dir = _resolve_dir(args)
    task_id: str = args.id

    patch: dict[str, Any] = {}
    if getattr(args, "title", None) is not None:
        patch["title"] = args.title
    if getattr(args, "description", None) is not None:
        patch["description"] = args.description
    if getattr(args, "priority", None) is not None:
        patch["priority"] = args.priority
    if not patch:
        _err("arc task edit: nothing to update — pass --title/--description/--priority")
        sys.exit(2)

    async def _run() -> Task:
        from arcstore.tasks import Task
        from pydantic import ValidationError

        from arccli.commands.team import _build_service, _shutdown

        _, registry, _, team_backend = await _build_service(root)
        try:
            entity = await _operator_entity(registry, args.actor)
        finally:
            await _shutdown(team_backend)

        store, store_backend = await _open_store(data_dir, mutable=True)
        try:
            current = await store.get(task_id)
            if current is None:
                _err(f"arc task edit: task not found: {task_id}")
                sys.exit(1)
            # SEC-F2: a partial patch never constructs a full Task, so validate
            # the patched free-text through the model before the store write.
            try:
                Task(**{**current.model_dump(), **patch})
            except ValidationError:
                _err(f"arc task edit: rejected — invalid field value for {task_id}")
                sys.exit(1)
            # store.edit is the status-conditional at-rest write: it refuses an
            # in_progress task and rejects one that raced into in_progress
            # between the read and the write (REL-F1), rather than clobbering it.
            updated, outcome = await store.edit(task_id, patch, actor_did=str(entity.did))
            if outcome in ("in_progress", "conflict"):
                _err(
                    f"arc task edit: task {task_id} is in_progress — "
                    "steer it with `arc task talk`, not edit"
                )
                sys.exit(1)
            if updated is None:
                _err(f"arc task edit: task vanished mid-update: {task_id}")
                sys.exit(1)
            return updated
        finally:
            await store_backend.stop()

    updated = asyncio.run(_run())
    if getattr(args, "json", False):
        _out(json.dumps(updated.model_dump(mode="json")))
    else:
        _out(f"Updated task {task_id}")


# ---------------------------------------------------------------------------
# assign
# ---------------------------------------------------------------------------


def _assign(args: argparse.Namespace) -> None:
    root = _get_root(args)
    data_dir = _resolve_dir(args)
    task_id: str = args.id

    async def _run() -> Task | None:
        from arcteam.registry import resolve

        from arccli.commands.team import _build_service, _shutdown

        _, registry, _, team_backend = await _build_service(root)
        try:
            entity = await _operator_entity(registry, args.actor)
            to_did = await resolve(registry, args.owner_ref)
        finally:
            await _shutdown(team_backend)

        store, store_backend = await _open_store(data_dir, mutable=True)
        try:
            return await store.assign(task_id, to_did, str(entity.did))
        finally:
            await store_backend.stop()

    updated = asyncio.run(_run())
    if updated is None:
        _err(f"arc task assign: refused — task {task_id} not found or in_progress")
        sys.exit(1)
    _out(f"Assigned {task_id} -> {updated.owner_did}")


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


def _complete(args: argparse.Namespace) -> None:
    root = _get_root(args)
    data_dir = _resolve_dir(args)
    task_id: str = args.id
    resolution: str | None = getattr(args, "resolution", None)

    patch: dict[str, Any] = {"status": "done"}
    if resolution is not None:
        patch["resolution"] = resolution

    async def _run() -> Task:
        from arccli.commands.team import _build_service, _shutdown

        _, registry, _, team_backend = await _build_service(root)
        try:
            entity = await _operator_entity(registry, args.actor)
        finally:
            await _shutdown(team_backend)

        store, store_backend = await _open_store(data_dir, mutable=True)
        try:
            current = await store.get(task_id)
            if current is None:
                _err(f"arc task complete: task not found: {task_id}")
                sys.exit(1)
            updated = await store.update(task_id, patch, actor_did=str(entity.did))
            if updated is None:
                _err(f"arc task complete: task vanished mid-update: {task_id}")
                sys.exit(1)
            return updated
        finally:
            await store_backend.stop()

    updated = asyncio.run(_run())
    if getattr(args, "json", False):
        _out(json.dumps(updated.model_dump(mode="json")))
    else:
        _out(f"Completed {task_id}")


# ---------------------------------------------------------------------------
# talk — steer the owner; NOT a task edit
# ---------------------------------------------------------------------------


def _talk(args: argparse.Namespace) -> None:
    root = _get_root(args)
    data_dir = _resolve_dir(args)
    task_id: str = args.id
    body: str = args.message

    async def _run() -> Any:
        from arcteam.messenger import MessagingService
        from arcteam.types import Message

        from arccli.commands.team import _build_service, _shutdown, _signer_for

        _, registry, audit, team_backend = await _build_service(root)
        try:
            await _operator_entity(registry, args.actor)

            store, store_backend = await _open_store(data_dir, mutable=False)
            try:
                task = await store.get(task_id)
            finally:
                await store_backend.stop()
            if task is None or task.owner_did is None:
                _err(f"arc task talk: task {task_id} has no owner to steer")
                sys.exit(1)

            owner = await registry.get(task.owner_did)
            if owner is None:
                _err(f"arc task talk: owner of task {task_id} is not a registered entity")
                sys.exit(1)
            target_uri = f"{owner.type.value}://{owner.handle}"

            signer = await _signer_for(registry, args.actor)
            svc = MessagingService(team_backend, registry, audit, signer=signer)
            message = Message(sender=args.actor, to=[target_uri], body=body)
            return await svc.send(message)
        finally:
            await _shutdown(team_backend)

    sent = asyncio.run(_run())
    _out(f"Sent: {sent.id} (seq={sent.seq})")


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

_SUBCOMMANDS = {
    "create": _create,
    "list": _list,
    "edit": _edit,
    "assign": _assign,
    "complete": _complete,
    "talk": _talk,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc task",
        description="Mission-control tasks — create, list, edit, assign, complete, talk.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", default=None, help="arcteam data root (default: TeamConfig).")
        p.add_argument(
            "--data-dir",
            dest="data_dir",
            default=None,
            help="Arc data dir (default: env ARCSTORE_DATA_DIR or ~/.arc/store).",
        )

    create_p = subs.add_parser("create", help="Create a task.")
    _common(create_p)
    create_p.add_argument("title")
    create_p.add_argument("--actor", required=True, help="Operator ref (handle/DID/URI).")
    create_p.add_argument("--owner", default=None, help="Owner ref; unset creates unowned.")
    create_p.add_argument("--priority", default=None, choices=_PRIORITIES)
    create_p.add_argument("--description", default="")
    create_p.add_argument("--json", action="store_true", help="Emit JSON.")

    list_p = subs.add_parser("list", help="List tasks.")
    _common(list_p)
    list_p.add_argument("--scope", default=None, choices=("mine",))
    list_p.add_argument("--status", default=None)
    list_p.add_argument("--owner", default=None, help="Owner ref to filter by.")
    list_p.add_argument("--actor", default=None, help="Required for --scope mine.")
    list_p.add_argument("--json", action="store_true", help="Emit JSON.")

    edit_p = subs.add_parser("edit", help="Edit a task at rest.")
    _common(edit_p)
    edit_p.add_argument("id")
    edit_p.add_argument("--actor", required=True, help="Operator ref (handle/DID/URI).")
    edit_p.add_argument("--title", default=None)
    edit_p.add_argument("--description", default=None)
    edit_p.add_argument("--priority", default=None, choices=_PRIORITIES)
    edit_p.add_argument("--json", action="store_true", help="Emit JSON.")

    assign_p = subs.add_parser("assign", help="Assign a task to a teammate.")
    _common(assign_p)
    assign_p.add_argument("id")
    assign_p.add_argument("owner_ref", help="New owner ref (e.g. @bob).")
    assign_p.add_argument("--actor", required=True, help="Operator ref (handle/DID/URI).")

    complete_p = subs.add_parser("complete", help="Mark a task done.")
    _common(complete_p)
    complete_p.add_argument("id")
    complete_p.add_argument("--actor", required=True, help="Operator ref (handle/DID/URI).")
    complete_p.add_argument("--resolution", default=None)
    complete_p.add_argument("--json", action="store_true", help="Emit JSON.")

    talk_p = subs.add_parser("talk", help="Steer an in-flight task's owner (not an edit).")
    _common(talk_p)
    talk_p.add_argument("id")
    talk_p.add_argument("message")
    talk_p.add_argument("--actor", required=True, help="Operator ref (handle/DID/URI).")

    return parser


def task_handler(args: list[str]) -> None:
    """Entry point for ``arc task <subcommand>`` (registry dispatch)."""
    dispatch(_build_parser(), _SUBCOMMANDS, args)
