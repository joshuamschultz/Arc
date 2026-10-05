"""The sync worker process: the only writer of connected-data document stores.

Started and supervised by the main process (``supervisor.py``) and never by an
operator. It reads one bootstrap line from stdin (the per-spawn secret and its
two socket paths), then serves authenticated store writes until its parent goes
away: stdin reaching EOF, a changed parent pid, or (on Linux) the parent-death
signal. It never outlives the process that started it.

The worker holds no provider credential, no database credential and no private
key, and calls no provider. It writes the store files and their SQLite indexes,
and asks the main process (``host.py``) for a seal signature and the arcstore
rows a write reads or advances.

**Which stores.** A request names a store, and the worker derives the store's
root itself and refuses a request whose root disagrees:

* an agent's own store is the workspace of the agent whose ``arcagent.toml``
  (named by the request) declares the requesting DID;
* a connection's shared store is ``connected_knowledge_dir() / store_key(id)``.

**Who may write.** An agent's own store: its approval rows, re-read through the
host. A shared store: the writing agent's live subscription, asked before every
object and before every other write; or, while it migrates in, its own approved
document mapping. A store nobody reads any more may only be purged, and only
once the host confirms no subscription is left.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import logging
import os
import shutil
import signal
import sys
import time
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from arctrust.audit import AuditEvent
from arctrust.paths import connected_knowledge_dir

from arcagent.connected_data import MappingPlan
from arcagent.extension.knowledge_subscriptions import knowledge_principal, store_key
from arcagent.extension.source import SourceContent, SourceDescription, SourceObject
from arcagent.modules.connected_data.ingest import ArcMemoryIngestAdapter, embedder_from
from arcagent.modules.connected_data.shared import claim_store_profile
from arcagent.modules.connected_data.sync_worker.host import (
    HostApprovals,
    HostClient,
    HostMigrationAuthority,
    HostObjectState,
    HostSubscriberAuthority,
    NoWrites,
    RemoteSealSigner,
    RequestAuditSink,
    collect_request_events,
    stop_collecting,
)
from arcagent.modules.connected_data.sync_worker.protocol import (
    HOST_REQUEST,
    HOST_RESPONSE,
    WORKER_REQUEST,
    WORKER_RESPONSE,
)
from arcagent.modules.connected_data.sync_worker.rpc import RpcClient, RpcError, RpcServer
from arcagent.modules.connected_data.sync_worker.specs import StoreSpec, error_to_wire

_logger = logging.getLogger("arcagent.modules.connected_data.sync_worker.worker")

#: A store adapter unused this long has its SQLite connection closed.
_IDLE_SECONDS = 120.0
#: How often the worker checks that its parent is still the same process.
_PARENT_POLL_SECONDS = 1.0
#: The actor the worker's own audit events are filed under.
WORKER_DID = "did:arc:sync-worker"


class StoreRefusedError(RpcError):
    """A write the worker will not do: the store or its authority did not check."""

    def __init__(self, reason: str) -> None:
        super().__init__("refused", f"sync worker refused the write: {reason}", reason=reason)


class AgentConfigs:
    """An agent's DID and workspace, read from its own ``arcagent.toml``."""

    def __init__(self) -> None:
        self._cache: dict[Path, tuple[float, str, Path]] = {}

    def workspace_for(self, config_path: str, agent_did: str) -> Path | None:
        """The workspace of the agent at ``config_path``, if that agent is ``agent_did``."""
        if not config_path:
            return None
        path = Path(config_path).resolve()
        if path.name != "arcagent.toml" or not path.is_file():
            return None
        mtime = path.stat().st_mtime
        cached = self._cache.get(path)
        if cached is None or cached[0] != mtime:
            try:
                cfg = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                return None
            identity = cfg.get("identity")
            agent = cfg.get("agent")
            did = identity.get("did", "") if isinstance(identity, dict) else ""
            raw = agent.get("workspace", "./workspace") if isinstance(agent, dict) else ""
            workspace = Path(str(raw or "./workspace"))
            resolved = (
                workspace if workspace.is_absolute() else path.parent / workspace
            ).resolve()
            cached = (mtime, str(did), resolved)
            self._cache[path] = cached
        return cached[2] if cached[1] == agent_did and agent_did else None


@dataclass
class _Store:
    adapter: ArcMemoryIngestAdapter
    spec: StoreSpec
    authority: Any
    last_used: float
    in_use: int = 0


_Method = Callable[["StoreWriters", _Store, dict[str, Any], bytes], Awaitable[tuple[Any, bytes]]]

#: Which methods each authority may call.
_ALLOWED: dict[str, frozenset[str]] = {
    "owner": frozenset(
        {
            "require_approved_mapping",
            "ingest",
            "complete_snapshot",
            "finish_sync",
            "refresh_operator_guide",
            "relayout_source",
            "reset_source",
            "purge_source",
            "adopt_documents",
            "maintain_embeddings",
        }
    ),
    "subscriber": frozenset(
        {
            "require_approved_mapping",
            "ingest",
            "complete_snapshot",
            "finish_sync",
            "refresh_operator_guide",
            "relayout_source",
            "reset_source",
            "maintain_embeddings",
        }
    ),
    "migration": frozenset({"adopt_documents", "claim_profile", "require_approved_mapping"}),
    "orphan": frozenset({"purge_source", "drop_store"}),
}
#: Methods arcmemory authorizes per object itself; every other shared-store write
#: is checked against the authority here first.
_SELF_AUTHORIZED = frozenset({"require_approved_mapping", "ingest"})


class StoreWriters:
    """Open, authorize and write the stores requests name."""

    def __init__(
        self,
        host: HostClient,
        *,
        configs: AgentConfigs | None = None,
        knowledge_root: Callable[[], Path] = connected_knowledge_dir,
        idle_seconds: float = _IDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._host = host
        self._configs = configs or AgentConfigs()
        self._knowledge_root = knowledge_root
        self._idle = idle_seconds
        self._clock = clock
        self._audit = RequestAuditSink(host)
        self._stores: dict[str, _Store] = {}
        self._embedders: dict[tuple[str, tuple[str, str, str]], Any] = {}
        self._warmed: dict[tuple[str, tuple[str, str, str]], bool] = {}
        self._held: dict[Path, tuple[str, Callable[[], None]]] = {}

    async def call(
        self, spec: StoreSpec, method: str, args: dict[str, Any], body: bytes
    ) -> tuple[Any, bytes]:
        handler = _METHODS.get(method)
        if handler is None:
            raise StoreRefusedError(f"unknown_method:{method}")
        if method not in _ALLOWED.get(spec.authority, frozenset()):
            raise StoreRefusedError(f"method_not_allowed:{spec.authority}:{method}")
        store = self._open(spec)
        store.in_use += 1
        try:
            if spec.kind == "shared" and method not in _SELF_AUTHORIZED:
                await self._authorize(store, method)
            return await handler(self, store, args, body)
        finally:
            store.in_use -= 1
            store.last_used = self._clock()

    async def evict_idle(self) -> None:
        """Close the stores nobody wrote for a while; they reopen on the next write."""
        now = self._clock()
        for key, store in list(self._stores.items()):
            if store.in_use == 0 and now - store.last_used >= self._idle:
                self._stores.pop(key, None)
                await store.adapter.aclose()

    async def forget(self, store: _Store) -> None:
        """Close one store and drop it from the cache (it is being deleted)."""
        self._stores = {key: kept for key, kept in self._stores.items() if kept is not store}
        await store.adapter.aclose()

    async def close(self) -> None:
        stores, self._stores = list(self._stores.values()), {}
        for store in stores:
            await store.adapter.aclose()
        for _, release in self._held.values():
            release()
        self._held.clear()

    # -- resolving and authorizing a store ---------------------------------

    def _open(self, spec: StoreSpec) -> _Store:
        key = spec.model_dump_json()
        store = self._stores.get(key)
        if store is not None:
            return store
        root, owner, authority = self._resolve(spec)
        self._hold_seal(root, spec)
        adapter = ArcMemoryIngestAdapter(
            root,
            owner,
            approval_store=HostApprovals(self._host, spec.agent_did)
            if spec.kind == "own"
            else None,
            object_state=HostObjectState(self._host, owner),
            embedder=self._embedder(spec),
            audit_sink=self._audit,
            authority=authority,
            # A shared store can be rebuilt from its provider: no per-commit fsync.
            durability="normal" if spec.kind == "shared" else "full",
        )
        store = _Store(adapter, spec, authority, self._clock())
        self._stores[key] = store
        return store

    def _resolve(self, spec: StoreSpec) -> tuple[Path, str, Any]:
        """The store's root, owner and authority, derived here; never trusted from the request."""
        requested = Path(spec.root).resolve()
        if spec.kind == "own":
            workspace = self._configs.workspace_for(spec.config_path, spec.agent_did)
            if workspace is None or workspace != requested or spec.authority != "owner":
                raise StoreRefusedError("store_root_not_allowed")
            return workspace, spec.agent_did, None
        if not spec.connection_id:
            raise StoreRefusedError("store_root_not_allowed")
        root = (self._knowledge_root() / store_key(spec.connection_id)).resolve()
        if root != requested:
            raise StoreRefusedError("store_root_not_allowed")
        return root, knowledge_principal(spec.connection_id), self._authority(spec)

    def _authority(self, spec: StoreSpec) -> Any:
        if spec.authority == "subscriber":
            return HostSubscriberAuthority(
                self._host, spec.agent_did, spec.connection_id, spec.approval_id
            )
        if spec.authority == "migration":
            return HostMigrationAuthority(self._host, spec.agent_did, spec.approval_id)
        if spec.authority == "orphan":
            return NoWrites()
        raise StoreRefusedError("store_root_not_allowed")

    async def _authorize(self, store: _Store, method: str) -> None:
        spec = store.spec
        if spec.authority == "orphan":
            readers = await self._host.call(
                "subscriptions.for_connection", {"connection_id": spec.connection_id}
            )
            if readers:
                raise StoreRefusedError("store_still_read")
            return
        if await store.authority.authorized_homes() is None:
            raise StoreRefusedError("not_authorized")

    def _hold_seal(self, root: Path, spec: StoreSpec) -> None:
        """Pin the store's seal key for the worker's life; the main process signs."""
        if spec.seal is None:
            return
        held = self._held.get(root)
        if held is not None and held[0] == spec.seal.public_key:
            return
        try:
            seal = import_module("arcmemory.okf_seal")
        except ImportError:
            return
        release = seal.hold_memory_identity(root, RemoteSealSigner(self._host, root, spec.seal))
        if held is not None:
            held[1]()
        self._held[root] = (spec.seal.public_key, release)

    def _embedder(self, spec: StoreSpec) -> Any | None:
        return None if spec.embed is None else self._embedder_for(spec.agent_did, spec.embed)

    def _embedder_for(self, agent_did: str, embed: tuple[str, str, str]) -> Any | None:
        key = (agent_did, embed)
        if key not in self._embedders:
            self._embedders[key] = embedder_from(agent_did, embed)
        return self._embedders[key]

    @property
    def embedder_count(self) -> int:
        """How many agents' embedders this worker holds (reported by ``ping``)."""
        return len(self._embedders)

    async def warm(self, agent_did: str, embed: tuple[str, str, str]) -> bool:
        """Load one agent's local embedding model now, before its first write.

        A cold load (torch plus weights) takes seconds, and inside a sync run it
        would count against the run's stall budget as if the provider had hung.
        Only the on-device backend is warmed: a remote one would be a provider
        call (and egress) at every agent start, and has nothing to load.
        """
        if embed[0] != "local":
            return False
        key = (agent_did, embed)
        warmed = self._warmed.get(key)
        if warmed is None:
            embedder = self._embedder_for(agent_did, embed)
            if embedder is None:  # no arcmemory installed: the store is lexical only
                return False
            rebuild = import_module("arcmemory.index.rebuild")
            vectors = await rebuild.embed_or_none(embedder, ["warm"], operation="embed:warm")
            warmed = self._warmed[key] = vectors is not None
        return warmed


# -- the methods a request may call ------------------------------------------


def _source(args: dict[str, Any], key: str = "source") -> SourceDescription:
    return SourceDescription.model_validate(args[key])


def _mapping(args: dict[str, Any]) -> MappingPlan:
    return MappingPlan.model_validate(args["mapping"])


async def _require(
    writers: StoreWriters, store: _Store, args: dict[str, Any], body: bytes
) -> tuple[Any, bytes]:
    plan = await store.adapter.require_approved_mapping(_source(args))
    return plan.model_dump(mode="json"), b""


async def _ingest(
    writers: StoreWriters, store: _Store, args: dict[str, Any], body: bytes
) -> tuple[Any, bytes]:
    raw = args.get("content")
    content = None if raw is None else SourceContent.model_validate({**raw, "content": body})
    await store.adapter.ingest(
        _source(args), SourceObject.model_validate(args["object"]), content, _mapping(args)
    )
    return None, b""


async def _complete(
    writers: StoreWriters, store: _Store, args: dict[str, Any], body: bytes
) -> tuple[Any, bytes]:
    ids = frozenset(str(item) for item in args["object_ids"])
    await store.adapter.complete_snapshot(_source(args), ids, _mapping(args))
    return None, b""


async def _finish(
    writers: StoreWriters, store: _Store, args: dict[str, Any], body: bytes
) -> tuple[Any, bytes]:
    await store.adapter.finish_sync(_source(args))
    return None, b""


async def _guide(
    writers: StoreWriters, store: _Store, args: dict[str, Any], body: bytes
) -> tuple[Any, bytes]:
    return await store.adapter.refresh_operator_guide(_source(args)), b""


async def _relayout(
    writers: StoreWriters, store: _Store, args: dict[str, Any], body: bytes
) -> tuple[Any, bytes]:
    return await store.adapter.relayout_source(_source(args)), b""


async def _reset(
    writers: StoreWriters, store: _Store, args: dict[str, Any], body: bytes
) -> tuple[Any, bytes]:
    await store.adapter.reset_source(_source(args))
    return None, b""


async def _purge(
    writers: StoreWriters, store: _Store, args: dict[str, Any], body: bytes
) -> tuple[Any, bytes]:
    await store.adapter.purge_source(_source(args))
    return None, b""


async def _adopt(
    writers: StoreWriters, store: _Store, args: dict[str, Any], body: bytes
) -> tuple[Any, bytes]:
    donor_spec = StoreSpec.model_validate(args["donor"])
    if donor_spec.kind != "own" or donor_spec.agent_did != store.spec.agent_did:
        raise StoreRefusedError("donor_not_the_writers_own_store")
    donor = writers._open(donor_spec)
    counts = await store.adapter.adopt_documents(
        _source(args),
        donor.adapter,
        _source(args, "donor_source"),
        dry_run=bool(args.get("dry_run", False)),
    )
    return counts, b""


async def _maintain(
    writers: StoreWriters, store: _Store, args: dict[str, Any], body: bytes
) -> tuple[Any, bytes]:
    return await store.adapter.maintain_embeddings(), b""


async def _claim(
    writers: StoreWriters, store: _Store, args: dict[str, Any], body: bytes
) -> tuple[Any, bytes]:
    return claim_store_profile(store.adapter.workspace, str(args["profile"])), b""


async def _drop(
    writers: StoreWriters, store: _Store, args: dict[str, Any], body: bytes
) -> tuple[Any, bytes]:
    root = store.adapter.workspace
    await writers.forget(store)
    await asyncio.to_thread(shutil.rmtree, root, True)
    return None, b""


_METHODS: dict[str, _Method] = {
    "require_approved_mapping": _require,
    "ingest": _ingest,
    "complete_snapshot": _complete,
    "finish_sync": _finish,
    "refresh_operator_guide": _guide,
    "relayout_source": _relayout,
    "reset_source": _reset,
    "purge_source": _purge,
    "adopt_documents": _adopt,
    "maintain_embeddings": _maintain,
    "claim_profile": _claim,
    "drop_store": _drop,
}


# -- serving -----------------------------------------------------------------


class WorkerHandler:
    """Answer ``ping``, ``warm`` and ``write``; everything else is refused."""

    def __init__(self, writers: StoreWriters) -> None:
        self._writers = writers
        self._started = time.time()

    async def __call__(self, op: str, args: dict[str, Any], body: bytes) -> tuple[Any, bytes]:
        if op == "ping":
            return {
                "pid": os.getpid(),
                "started": self._started,
                "embedders": self._writers.embedder_count,
            }, b""
        if op == "warm":
            return {"warmed": await self._warm(args)}, b""
        if op != "write":
            raise RpcError("unknown_op", op)
        try:
            spec = StoreSpec.model_validate(args.get("store"))
        except ValueError as exc:
            raise StoreRefusedError("malformed_store") from exc
        method = str(args.get("method", ""))
        call_args = args.get("args")
        events, token = collect_request_events()
        try:
            value, out = await self._writers.call(
                spec, method, call_args if isinstance(call_args, dict) else {}, body
            )
        except Exception as exc:
            wire = error_to_wire(exc)
            wire.wire["audit"] = events
            if wire.wire["type"] == "write_failed":
                # Only an unexpected failure; a pending mapping or an unreadable
                # object is an answer the caller acts on, not a fault.
                _logger.warning("sync worker %s failed", method, exc_info=True)
            raise wire from exc
        finally:
            stop_collecting(token)
        return {"value": value, "audit": events}, out

    async def _warm(self, args: dict[str, Any]) -> bool:
        agent_did, embed = args.get("agent_did"), args.get("embed")
        if (
            not isinstance(agent_did, str)
            or not agent_did
            or not isinstance(embed, list)
            or len(embed) != 3
            or not all(isinstance(part, str) for part in embed)
        ):
            raise RpcError("malformed_warm")
        return await self._writers.warm(agent_did, (embed[0], embed[1], embed[2]))


def _refusal_reporter(host: HostClient) -> Callable[[str], None]:
    pending: set[asyncio.Task[None]] = set()

    def refused(reason: str) -> None:
        _logger.warning("sync worker refused a request: %s", reason)
        event = AuditEvent(
            actor_did=WORKER_DID,
            action="connected_data.sync_worker.request_refused",
            target="sync-worker",
            outcome="deny",
            extra={"reason": reason[:200]},
        )
        task = asyncio.get_running_loop().create_task(_quiet(host.audit(event)))
        pending.add(task)
        task.add_done_callback(pending.discard)

    return refused


async def _quiet(call: Awaitable[None]) -> None:
    with contextlib.suppress(Exception):
        await call


def _die_with_parent() -> None:
    """Linux: have the kernel send SIGTERM when the parent dies, even by SIGKILL."""
    if not sys.platform.startswith("linux"):
        return
    with contextlib.suppress(OSError, AttributeError):
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        pr_set_pdeathsig = 1
        libc.prctl(pr_set_pdeathsig, signal.SIGTERM)


async def _until_parent_gone(parent_pid: int, stop: asyncio.Event) -> None:
    """Return once stdin reaches EOF, the parent pid changes, or a stop was asked."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    with contextlib.suppress(OSError, ValueError):
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    async def stdin_eof() -> None:
        while await reader.read(1 << 16):
            pass

    async def parent_changed() -> None:
        while os.getppid() == parent_pid:
            await asyncio.sleep(_PARENT_POLL_SECONDS)

    waits = [
        asyncio.create_task(stdin_eof()),
        asyncio.create_task(parent_changed()),
        asyncio.create_task(stop.wait()),
    ]
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in waits:
            task.cancel()
        await asyncio.gather(*waits, return_exceptions=True)


async def serve(bootstrap: dict[str, Any]) -> None:
    """Serve store writes until the parent goes away."""
    secret = bytes.fromhex(str(bootstrap["secret"]))
    parent_pid = int(bootstrap["parent_pid"])
    host = HostClient(
        RpcClient(
            path=Path(str(bootstrap["host_socket"])),
            secret=secret,
            request_direction=HOST_REQUEST,
            response_direction=HOST_RESPONSE,
            expected_pid=parent_pid,
        )
    )
    writers = StoreWriters(host)
    server = RpcServer(
        path=Path(str(bootstrap["socket"])),
        secret=lambda: secret,
        request_direction=WORKER_REQUEST,
        response_direction=WORKER_RESPONSE,
        handler=WorkerHandler(writers),
        expected_pid=lambda: parent_pid,
        on_refused=_refusal_reporter(host),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, stop.set)

    async def janitor() -> None:
        while True:
            await asyncio.sleep(_IDLE_SECONDS / 4)
            await writers.evict_idle()

    await server.start()
    sweeper = asyncio.create_task(janitor())
    try:
        await _until_parent_gone(parent_pid, stop)
    finally:
        sweeper.cancel()
        await asyncio.gather(sweeper, return_exceptions=True)
        await server.close()
        await writers.close()


def main(argv: list[str] | None = None) -> None:
    """``arc sync-worker``: started by the main process, never by hand."""
    del argv
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s sync-worker %(message)s"
    )
    line = sys.stdin.buffer.readline()
    try:
        bootstrap = json.loads(line.decode("utf-8"))
    except (UnicodeError, ValueError):
        sys.stderr.write("sync-worker: no bootstrap on stdin; it is started by `arc ui start`\n")
        raise SystemExit(2) from None
    _die_with_parent()
    if os.getppid() != int(bootstrap.get("parent_pid", -1)):
        raise SystemExit(0)  # the parent died before we could watch it
    asyncio.run(serve(bootstrap))


__all__ = [
    "WORKER_DID",
    "AgentConfigs",
    "StoreRefusedError",
    "StoreWriters",
    "WorkerHandler",
    "main",
    "serve",
]
