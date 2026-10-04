"""Agent-signed seal over one collection's OKF sidecars (item 62 F4).

``.index.digest`` and ``.log.digest`` are plain SHA-256 records: anyone who can
write ``index.md`` can recompute its sidecar, so on their own they prove only
that two files agree. The seal makes them authentic. One ``.okf.seal`` sits at
each collection root (``memory/`` and every ``memory/connected/<source>/``) and
is signed with the AGENT's identity key (the agent owns its memory state,
ADR-029; the operator key never signs here). A connection's shared store, which
no agent owns, is signed by the connection's knowledge principal instead; each
agent that opens it holds that key with :func:`hold_memory_identity`. It commits to:

* the agent DID, algorithm and public key it was signed with;
* the collection path relative to the bound workspace (a seal copied to another
  collection or another agent does not verify);
* a generation counter that only grows, so an older seal replayed in this
  process is refused;
* the SHA-256 of every folder's ``.index.digest`` and of the ``.log.digest``;
* an optional ``pending`` intent written before the files of a drain, so a
  crash between the index and the log writes is replayed, never lost.

A reader trusts a folder only when its sidecar hash is in a seal that verifies
against the pinned key. The key is pinned in-process by the agent's brain
(:func:`bind_memory_identity`); a process with no pinned key trusts nothing and
every reader fails closed.

Residual (documented, as for ``FileJournalAnchor``): the highest generation is
remembered per process. A whole-tree rollback (seal, sidecars and files) made
before a process has seen the newer seal is not detected; the documents are
still the source of truth for labels and recall content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from arcokf import LogEntry, read_regular_file
from arctrust import canonical_json
from arctrust.signer import verify_signature

from arcmemory.mdfile import atomic_write_text

_logger = logging.getLogger("arcmemory.okf_seal")

SEAL_NAME = ".okf.seal"
_KIND = "arc.okf.seal"
_VERSION = 1
_DOMAIN = b"arc.okf.seal.v1\n"


class SealSigner(Protocol):
    """The agent's signing capability (``arctrust.identity.AgentIdentity`` fits)."""

    @property
    def did(self) -> str: ...

    @property
    def public_key(self) -> bytes: ...

    @property
    def algorithm(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


#: (device, inode, mtime_ns, size) of a seal file this process already verified.
_FileStamp = tuple[int, int, int, int]


@dataclass
class _Binding:
    signer: SealSigner
    high_water: dict[str, int] = field(default_factory=dict)
    #: The newest verified seal per collection and the file stamp it was read at,
    #: so a heal pass over thousands of folders verifies the seal once, not per
    #: folder. Only verified seals are ever cached, and the replay check still
    #: runs on every load.
    verified: dict[str, tuple[_FileStamp, Seal]] = field(default_factory=dict)
    #: Owners holding this binding through :func:`hold_memory_identity`.
    holders: int = 0


@dataclass(frozen=True, slots=True)
class SealPending:
    """What one drain is about to write: new sidecar hashes and its log events."""

    folders: dict[str, str]
    log: str
    events: tuple[LogEntry, ...]


@dataclass(frozen=True, slots=True)
class Seal:
    """A verified seal: the committed sidecar hashes of one collection."""

    generation: int
    folders: dict[str, str]
    log: str
    pending: SealPending | None = None

    def trusts_folder(self, rel: str, sidecar_sha: str) -> bool:
        """Whether a folder sidecar with this hash was written by the agent."""
        if not sidecar_sha:
            return False
        if self.folders.get(rel) == sidecar_sha:
            return True
        return self.pending is not None and self.pending.folders.get(rel) == sidecar_sha

    def trusts_log(self, sidecar_sha: str) -> bool:
        """Whether a ``.log.digest`` with this hash was written by the agent."""
        if not sidecar_sha:
            return False
        if self.log == sidecar_sha:
            return True
        return self.pending is not None and self.pending.log == sidecar_sha


_BINDINGS: dict[str, _Binding] = {}
_LOCK = threading.Lock()


def bind_memory_identity(workspace: Path, signer: SealSigner) -> None:
    """Pin ``signer`` as the key that signs and verifies memory under ``workspace``.

    Called by the agent's brain at construction. Every collection under the
    workspace resolves to the nearest bound ancestor.
    """
    with _LOCK:
        current = _BINDINGS.get(_key(workspace))
        if current is not None and current.signer.public_key == signer.public_key:
            current.signer = signer
            return
        _BINDINGS[_key(workspace)] = _Binding(signer)


def release_memory_identity(workspace: Path) -> None:
    """Drop the pinned key for ``workspace`` (teardown); readers then fail closed."""
    with _LOCK:
        _BINDINGS.pop(_key(workspace), None)


def hold_memory_identity(root: Path, signer: SealSigner) -> Callable[[], None]:
    """Pin ``signer`` for ``root`` on behalf of one of several owners; return its release.

    A root shared by several owners in one process (a connection's shared
    knowledge store, opened by each subscribed agent) is unbound only when the
    last holder releases. Each returned release drops its own hold once; a hold
    under a different key replaces the binding, and the stale holder's release
    then leaves the new binding alone.
    """
    key = _key(root)
    with _LOCK:
        binding = _BINDINGS.get(key)
        if binding is not None and binding.signer.public_key == signer.public_key:
            binding.signer = signer
        else:
            binding = _BINDINGS[key] = _Binding(signer)
        binding.holders += 1
    released = False

    def release() -> None:
        nonlocal released
        with _LOCK:
            if released:
                return
            released = True
            binding.holders -= 1
            if binding.holders <= 0 and _BINDINGS.get(key) is binding:
                del _BINDINGS[key]

    return release


def bound_signer(root: Path) -> SealSigner | None:
    """The key pinned for the collection at ``root`` in this process, if any.

    Exposes the signer's public half to a caller that must describe it to another
    process (the sync worker), which signs nothing itself.
    """
    bound = _binding_for(root)
    return None if bound is None else bound[0].signer


def sign_for(root: Path, message: bytes) -> bytes:
    """Sign one seal for a collection under ``root`` with this process's pinned key.

    The delegated half of :meth:`CollectionSeal.write` for a process that writes a
    store but holds no key (the sync worker). The key is never handed out: this
    signs, and only a well-formed seal payload for a collection inside ``root``
    under exactly the pinned identity, so the capability cannot be turned into a
    signer of arbitrary bytes (a confused deputy). Raises ``PermissionError``
    otherwise.
    """
    bound = _binding_for(root)
    if bound is None or not _can_sign(bound[0].signer):
        raise PermissionError(f"no agent signing key bound for {root}")
    binding, base = bound
    signer = binding.signer
    if not message.startswith(_DOMAIN):
        raise PermissionError("not a seal payload")
    try:
        document = json.loads(message[len(_DOMAIN) :].decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise PermissionError("not a seal payload") from exc
    if (
        not isinstance(document, dict)
        or canonical_json(document) != message[len(_DOMAIN) :]
        or document.get("kind") != _KIND
        or document.get("v") != _VERSION
        or not _collection_within(document.get("collection"), base)
        or document.get("did") != signer.did
        or document.get("algorithm") != signer.algorithm
        or document.get("public_key") != signer.public_key.hex()
    ):
        raise PermissionError("seal payload does not match the pinned collection key")
    return signer.sign(message)


def _collection_within(collection: Any, base: str) -> bool:
    """A seal's collection path lies at or below ``base`` (both relative to the binding)."""
    if not isinstance(collection, str) or not collection:
        return False
    parts = collection.split("/")
    if collection.startswith("/") or ".." in parts:
        return False
    return base == "." or collection == base or collection.startswith(base + "/")


def _key(path: Path) -> str:
    return str(Path(path).resolve())


def _binding_for(root: Path) -> tuple[_Binding, str] | None:
    """The binding covering ``root`` and the collection path relative to it."""
    absolute = Path(root).resolve()
    with _LOCK:
        for candidate in (absolute, *absolute.parents):
            binding = _BINDINGS.get(str(candidate))
            if binding is not None:
                rel = absolute.relative_to(candidate).as_posix()
                return binding, rel
    return None


def sha256_hex(raw: bytes) -> str:
    """Hex SHA-256 of ``raw`` (what a seal records for each sidecar)."""
    return hashlib.sha256(raw).hexdigest()


def _event_json(entry: LogEntry) -> dict[str, str]:
    return {
        "day": entry.day,
        "kind": entry.kind,
        "path": entry.path,
        "title": entry.title,
        "summary": entry.summary,
    }


def _event(raw: Any) -> LogEntry:
    item = dict(raw)
    return LogEntry(
        str(item["day"]),
        str(item["kind"]),
        str(item["path"]),
        str(item["title"]),
        str(item.get("summary", "")),
    )


class CollectionSeal:
    """Load, verify and write the seal of one collection root."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def path(self) -> Path:
        """The seal file at the collection root."""
        return self._root / SEAL_NAME

    @property
    def can_sign(self) -> bool:
        """Whether this process holds the agent key for this collection."""
        bound = _binding_for(self._root)
        return bound is not None and _can_sign(bound[0].signer)

    def load(self) -> Seal | None:
        """The verified seal, or ``None`` (absent, forged, foreign, replayed, no key)."""
        bound = _binding_for(self._root)
        if bound is None:
            return None
        binding, collection = bound
        stamp = _stamp(self.path)
        if stamp is None:
            return None
        cached = binding.verified.get(collection)
        if cached is not None and cached[0] == stamp:
            seal = cached[1]
        else:
            try:
                raw = read_regular_file(self.path)
            except OSError:
                return None
            verified = _verify(raw, binding.signer, collection)
            if verified is None:
                _logger.warning("okf seal at %s does not verify; collection untrusted", self.path)
                return None
            seal = verified
            binding.verified[collection] = (stamp, seal)
        with _LOCK:
            high = binding.high_water.get(collection, 0)
            if seal.generation < high:
                _logger.warning(
                    "okf seal at %s is a replay (generation %d < %d)",
                    self.path,
                    seal.generation,
                    high,
                )
                return None
            binding.high_water[collection] = seal.generation
        return seal

    def write(
        self, folders: dict[str, str], log: str, pending: SealPending | None, previous: Seal | None
    ) -> Seal:
        """Sign and write the next seal generation; raise if this process cannot sign."""
        bound = _binding_for(self._root)
        if bound is None or not _can_sign(bound[0].signer):
            raise PermissionError(f"no agent signing key bound for {self._root}")
        binding, collection = bound
        with _LOCK:
            floor = max(
                binding.high_water.get(collection, 0), previous.generation if previous else 0
            )
        seal = Seal(floor + 1, dict(sorted(folders.items())), log, pending)
        payload = _payload(binding.signer, collection, seal)
        signature = binding.signer.sign(_DOMAIN + canonical_json(payload))
        document = {**payload, "signature": signature.hex()}
        atomic_write_text(self.path, json.dumps(document, sort_keys=True, indent=1) + "\n")
        stamp = _stamp(self.path)
        with _LOCK:
            binding.high_water[collection] = seal.generation
            if stamp is not None:
                binding.verified[collection] = (stamp, seal)
        return seal


def _stamp(path: Path) -> _FileStamp | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def _can_sign(signer: SealSigner) -> bool:
    return bool(getattr(signer, "can_sign", True))


def _payload(signer: SealSigner, collection: str, seal: Seal) -> dict[str, Any]:
    pending: dict[str, Any] | None = None
    if seal.pending is not None:
        pending = {
            "events": [_event_json(e) for e in seal.pending.events],
            "folders": dict(sorted(seal.pending.folders.items())),
            "log": seal.pending.log,
        }
    return {
        "algorithm": signer.algorithm,
        "collection": collection,
        "did": signer.did,
        "folders": seal.folders,
        "generation": seal.generation,
        "kind": _KIND,
        "log": seal.log,
        "pending": pending,
        "public_key": signer.public_key.hex(),
        "v": _VERSION,
    }


def _verify(raw: bytes, signer: SealSigner, collection: str) -> Seal | None:
    """Parse and verify one seal against the pinned key and the expected collection."""
    try:
        document = json.loads(raw.decode("utf-8"))
        signature = bytes.fromhex(str(document.pop("signature")))
        if (
            document.get("kind") != _KIND
            or document.get("v") != _VERSION
            or document.get("collection") != collection
            or document.get("did") != signer.did
            or document.get("algorithm") != signer.algorithm
            or document.get("public_key") != signer.public_key.hex()
        ):
            return None
        message = _DOMAIN + canonical_json(document)
        if not verify_signature(signer.algorithm, message, signature, signer.public_key):
            return None
        pending_raw = document.get("pending")
        pending = None
        if pending_raw is not None:
            pending = SealPending(
                folders={str(k): str(v) for k, v in dict(pending_raw["folders"]).items()},
                log=str(pending_raw["log"]),
                events=tuple(_event(e) for e in pending_raw["events"]),
            )
        generation = document["generation"]
        if not isinstance(generation, int) or generation < 1:
            return None
        return Seal(
            generation=generation,
            folders={str(k): str(v) for k, v in dict(document["folders"]).items()},
            log=str(document["log"]),
            pending=pending,
        )
    except (KeyError, TypeError, ValueError, UnicodeError):
        return None


__all__ = [
    "SEAL_NAME",
    "CollectionSeal",
    "Seal",
    "SealPending",
    "SealSigner",
    "bind_memory_identity",
    "bound_signer",
    "hold_memory_identity",
    "release_memory_identity",
    "sha256_hex",
    "sign_for",
]
