"""Compressed ordered authenticated queue indexes beneath one external root."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast

KEY_LENGTH = 144
SCOPE_LENGTH = 64
JobRow = tuple[str, str, str, str, int, float, str]
_HEX = frozenset("0123456789abcdef")
_EMPTY_LEAF = hashlib.sha256(b"arc.queue.empty-leaf.v4").hexdigest()
_EMPTY_ROOT = hashlib.sha256(b"arc.queue.empty-root.v4").hexdigest()


def empty_leaf() -> str:
    """Return the canonical absent-row marker."""
    return _EMPTY_LEAF


def tenant_key(tenant_id: str) -> str:
    """Return the fixed-size tenant projection key."""
    return hashlib.sha256(b"arc.queue.tenant.v4\0" + tenant_id.encode()).hexdigest()


def owner_key(owner_id: str) -> str:
    """Return the fixed-size owner projection key."""
    return hashlib.sha256(b"arc.queue.owner.v4\0" + owner_id.encode()).hexdigest()


def scope_key(tenant: str, owner: str | None, state: str | None) -> str:
    """Name one exact tenant/owner/state listing view."""
    body = json.dumps([tenant, owner, state], separators=(",", ":")).encode()
    return hashlib.sha256(b"arc.queue.scope.v4\0" + body).hexdigest()


ID_SCOPE = hashlib.sha256(b"arc.queue.id-scope.v4").hexdigest()
GLOBAL_SCOPE = hashlib.sha256(b"arc.queue.global-scope.v4").hexdigest()
CONTROL_KEY = hashlib.sha256(b"arc.queue.control.v4").hexdigest() + "0" * 80


def _reverse(value: str) -> str:
    return bytes(byte ^ 0xFF for byte in bytes.fromhex(value)).hex()


def sort_suffix(updated: float, row_id: str) -> str:
    """Encode newest timestamp and descending row ID in ascending hex order."""
    if not math.isfinite(updated) or updated < 0:
        raise ValueError("invalid queue timestamp")
    if len(row_id) != 64:
        raise ValueError("invalid queue row id")
    return _reverse(struct.pack(">d", updated).hex()) + _reverse(row_id)


def index_keys(row: JobRow) -> tuple[str, ...]:
    """Bind listing scopes and direct ID lookup to one row."""
    row_id, tenant, owner, state, _, updated, _ = row
    suffix = sort_suffix(updated, row_id)
    return (
        GLOBAL_SCOPE + suffix,
        scope_key(tenant, None, None) + suffix,
        scope_key(tenant, owner, None) + suffix,
        scope_key(tenant, None, state) + suffix,
        scope_key(tenant, owner, state) + suffix,
        ID_SCOPE + row_id + "0" * 16,
    )


def id_key(row_id: str) -> str:
    """Locate a row independently of mutable sort and scope projections."""
    if len(row_id) != 64:
        raise ValueError("invalid queue row id")
    return ID_SCOPE + row_id + "0" * 16


def row_digest(key: str, row: JobRow) -> str:
    payload = json.dumps([key, row], separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(b"arc.queue.row.v4\0" + payload).hexdigest()


def control_digest(sealed: str) -> str:
    return hashlib.sha256(b"arc.queue.control-row.v4\0" + sealed.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class _Pointer:
    prefix: str
    digest: str


@dataclass(frozen=True, slots=True)
class _Node:
    pointer: _Pointer
    kind: str
    children: tuple[_Pointer, ...]


def _valid_prefix(prefix: str) -> bool:
    return len(prefix) <= KEY_LENGTH and all(char in _HEX for char in prefix)


def _branch_digest(prefix: str, children: tuple[_Pointer, ...]) -> str:
    payload = json.dumps(
        [prefix, [[child.prefix, child.digest] for child in children]],
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(b"arc.queue.branch.v4\0" + payload).hexdigest()


def _root_digest(pointer: _Pointer | None) -> str:
    if pointer is None:
        return _EMPTY_ROOT
    return hashlib.sha256(
        b"arc.queue.root.v4\0" + pointer.prefix.encode() + bytes.fromhex(pointer.digest)
    ).hexdigest()


def _root_pointer(db: sqlite3.Connection) -> _Pointer | None:
    row = db.execute("SELECT prefix, digest FROM queue_root WHERE id = 1").fetchone()
    if row is None:
        raise ValueError("queue root pointer missing")
    prefix, digest = row
    if prefix == "" and digest == "":
        return None
    if not _valid_prefix(prefix) or len(digest) != 64:
        raise ValueError("invalid queue root pointer")
    return _Pointer(prefix, digest)


def root(db: sqlite3.Connection) -> str:
    """Return the authenticated compressed tree root."""
    return _root_digest(_root_pointer(db))


def _save_root(db: sqlite3.Connection, pointer: _Pointer | None) -> None:
    db.execute(
        "UPDATE queue_root SET prefix = ?, digest = ? WHERE id = 1",
        (pointer.prefix if pointer else "", pointer.digest if pointer else ""),
    )


def _load(db: sqlite3.Connection, pointer: _Pointer) -> _Node:
    row = db.execute(
        "SELECT kind, digest, children FROM queue_nodes WHERE prefix = ?", (pointer.prefix,)
    ).fetchone()
    if row is None or row[1] != pointer.digest or not _valid_prefix(pointer.prefix):
        raise ValueError("queue proof node missing or mismatched")
    kind, _, encoded = row
    if kind == "leaf":
        if len(pointer.prefix) != KEY_LENGTH or encoded != "[]":
            raise ValueError("invalid queue leaf")
        return _Node(pointer, kind, ())
    if kind != "branch" or len(pointer.prefix) >= KEY_LENGTH:
        raise ValueError("invalid queue branch")
    raw = json.loads(encoded)
    if not isinstance(raw, list) or not 2 <= len(raw) <= 16:
        raise ValueError("invalid queue branch children")
    children: list[_Pointer] = []
    nibbles: list[str] = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError("invalid queue child pointer")
        child_prefix, child_digest = item
        if (
            not isinstance(child_prefix, str)
            or not isinstance(child_digest, str)
            or not _valid_prefix(child_prefix)
            or not child_prefix.startswith(pointer.prefix)
            or len(child_prefix) <= len(pointer.prefix)
            or len(child_digest) != 64
        ):
            raise ValueError("invalid queue child pointer")
        children.append(_Pointer(child_prefix, child_digest))
        nibbles.append(child_prefix[len(pointer.prefix)])
    if nibbles != sorted(set(nibbles)) or (
        _branch_digest(pointer.prefix, tuple(children)) != pointer.digest
    ):
        raise ValueError("queue branch digest mismatch")
    return _Node(pointer, kind, tuple(children))


def _save_leaf(db: sqlite3.Connection, key: str, digest: str) -> _Pointer:
    db.execute(
        "INSERT INTO queue_nodes(prefix, kind, digest, children) VALUES (?, 'leaf', ?, '[]') "
        "ON CONFLICT(prefix) DO UPDATE SET kind='leaf', digest=excluded.digest, children='[]'",
        (key, digest),
    )
    return _Pointer(key, digest)


def _save_branch(db: sqlite3.Connection, prefix: str, children: tuple[_Pointer, ...]) -> _Pointer:
    ordered = tuple(sorted(children, key=lambda child: child.prefix))
    digest = _branch_digest(prefix, ordered)
    encoded = json.dumps(
        [[child.prefix, child.digest] for child in ordered], separators=(",", ":")
    )
    db.execute(
        "INSERT INTO queue_nodes(prefix, kind, digest, children) VALUES (?, 'branch', ?, ?) "
        "ON CONFLICT(prefix) DO UPDATE SET kind='branch', digest=excluded.digest, "
        "children=excluded.children",
        (prefix, digest, encoded),
    )
    return _Pointer(prefix, digest)


def _split_prefix(first: str, second: str) -> str:
    common = 0
    for left, right in zip(first, second, strict=False):
        if left != right:
            break
        common += 1
    return first[:common]


def _build_tree(db: sqlite3.Connection, pointers: tuple[_Pointer, ...]) -> _Pointer | None:
    if not pointers:
        return None
    if len(pointers) == 1:
        return pointers[0]
    ordered = tuple(sorted(pointers, key=lambda pointer: pointer.prefix))
    prefix = _split_prefix(ordered[0].prefix, ordered[-1].prefix)
    groups: dict[str, list[_Pointer]] = {}
    for pointer in ordered:
        groups.setdefault(pointer.prefix[len(prefix)], []).append(pointer)
    children = tuple(_build_tree(db, tuple(group)) for _, group in sorted(groups.items()))
    return _save_branch(db, prefix, tuple(child for child in children if child is not None))


def _set_subtree_many(
    db: sqlite3.Connection, pointer: _Pointer | None, changes: dict[str, str]
) -> _Pointer | None:
    if not changes:
        return pointer
    if pointer is None:
        added = tuple(
            _save_leaf(db, key, digest) for key, digest in changes.items() if digest != _EMPTY_LEAF
        )
        return _build_tree(db, added)
    node = _load(db, pointer)
    if node.kind == "leaf":
        leaf_replacement = changes.get(pointer.prefix)
        if leaf_replacement == _EMPTY_LEAF:
            db.execute("DELETE FROM queue_nodes WHERE prefix = ?", (pointer.prefix,))
            retained = None
        elif leaf_replacement is not None and leaf_replacement != pointer.digest:
            retained = _save_leaf(db, pointer.prefix, leaf_replacement)
        else:
            retained = pointer
        added = tuple(
            _save_leaf(db, key, digest)
            for key, digest in changes.items()
            if key != pointer.prefix and digest != _EMPTY_LEAF
        )
        return _build_tree(db, ((retained,) if retained else ()) + added)
    matching = {key: digest for key, digest in changes.items() if key.startswith(pointer.prefix)}
    divergent = {
        key: digest for key, digest in changes.items() if not key.startswith(pointer.prefix)
    }
    children: list[_Pointer] = []
    if matching:
        groups: dict[str, dict[str, str]] = {}
        for key, digest in matching.items():
            groups.setdefault(key[len(pointer.prefix)], {})[key] = digest
        for child in node.children:
            nibble = child.prefix[len(pointer.prefix)]
            child_replacement = _set_subtree_many(db, child, groups.pop(nibble, {}))
            if child_replacement is not None:
                children.append(child_replacement)
        for group in groups.values():
            child_replacement = _set_subtree_many(db, None, group)
            if child_replacement is not None:
                children.append(child_replacement)
    else:
        children.extend(node.children)
    if matching:
        if len(children) < 2:
            db.execute("DELETE FROM queue_nodes WHERE prefix = ?", (pointer.prefix,))
            retained = children[0] if children else None
        else:
            retained = _save_branch(db, pointer.prefix, tuple(children))
    else:
        retained = pointer
    added = tuple(
        _save_leaf(db, key, digest) for key, digest in divergent.items() if digest != _EMPTY_LEAF
    )
    return _build_tree(db, ((retained,) if retained else ()) + added)


def set_leaves(db: sqlite3.Connection, changes: dict[str, str]) -> None:
    """Apply a bounded set of leaf changes with each shared branch rebuilt once."""
    for key, digest in changes.items():
        if len(key) != KEY_LENGTH or any(char not in _HEX for char in key) or len(digest) != 64:
            raise ValueError("invalid queue proof key or digest")
    _save_root(db, _set_subtree_many(db, _root_pointer(db), changes))


def _verified_root(db: sqlite3.Connection, anchored_root: str) -> _Pointer | None:
    pointer = _root_pointer(db)
    if _root_digest(pointer) != anchored_root:
        raise ValueError("queue root does not match anchor")
    return pointer


def verify_point(db: sqlite3.Connection, key: str, digest: str, anchored_root: str) -> None:
    """Prove one present or absent key through compressed authenticated branches."""
    if len(key) != KEY_LENGTH:
        raise ValueError("invalid queue proof key")
    pointer = _verified_root(db, anchored_root)
    while pointer is not None:
        node = _load(db, pointer)
        if node.kind == "leaf":
            if pointer.prefix == key and pointer.digest == digest:
                return
            if pointer.prefix != key and digest == _EMPTY_LEAF:
                return
            raise ValueError("queue leaf proof mismatch")
        if not key.startswith(pointer.prefix):
            if digest == _EMPTY_LEAF:
                return
            raise ValueError("queue point proof absent")
        nibble = key[len(pointer.prefix)]
        pointer = next(
            (child for child in node.children if child.prefix[len(node.pointer.prefix)] == nibble),
            None,
        )
    if digest != _EMPTY_LEAF:
        raise ValueError("queue point proof absent")


def _scope_pointer(db: sqlite3.Connection, scope: str, anchored_root: str) -> _Pointer | None:
    pointer = _verified_root(db, anchored_root)
    while pointer is not None:
        if pointer.prefix.startswith(scope):
            return pointer
        if not scope.startswith(pointer.prefix):
            return None
        node = _load(db, pointer)
        if node.kind == "leaf":
            return None
        nibble = scope[len(pointer.prefix)]
        pointer = next(
            (child for child in node.children if child.prefix[len(pointer.prefix)] == nibble),
            None,
        )
    return None


def scope_digest(db: sqlite3.Connection, scope: str, anchored_root: str) -> str:
    """Return a scope-local revision independent of foreign updates."""
    if len(scope) != SCOPE_LENGTH:
        raise ValueError("invalid queue scope")
    pointer = _scope_pointer(db, scope, anchored_root)
    if pointer is None:
        return hashlib.sha256(b"arc.queue.empty-scope.v4\0" + scope.encode()).hexdigest()
    return hashlib.sha256(
        b"arc.queue.scope-root.v4\0"
        + scope.encode()
        + pointer.prefix.encode()
        + bytes.fromhex(pointer.digest)
    ).hexdigest()


def page_keys(
    db: sqlite3.Connection,
    scope: str,
    anchored_root: str,
    *,
    after: str | None,
    limit: int,
) -> list[str]:
    """Prove the next ordered leaves with work bounded by page and branch depth."""
    if len(scope) != SCOPE_LENGTH or limit < 1:
        raise ValueError("invalid queue page proof")
    pointer = _scope_pointer(db, scope, anchored_root)
    found: list[str] = []

    def visit(current: _Pointer) -> None:
        if len(found) >= limit:
            return
        if after is not None and (
            current.prefix + "f" * (KEY_LENGTH - len(current.prefix)) <= after
        ):
            return
        node = _load(db, current)
        if node.kind == "leaf":
            if not current.prefix.startswith(scope):
                raise ValueError("queue leaf escaped scope")
            if after is None or current.prefix > after:
                found.append(current.prefix)
            return
        for child in node.children:
            visit(child)
            if len(found) >= limit:
                return

    if pointer is not None:
        visit(pointer)
    return found


def _row_for_id(db: sqlite3.Connection, key: str, row_id: str) -> JobRow:
    row = db.execute(
        "SELECT id, tenant_key, owner_key, state, version, updated, sealed FROM jobs WHERE id = ?",
        (row_id,),
    ).fetchone()
    if row is None or key not in index_keys(row):
        raise ValueError("queue indexed row missing or mismatched")
    node = _load(db, _Pointer(key, row_digest(key, row)))
    if node.kind != "leaf":
        raise ValueError("queue indexed row is not a leaf")
    return cast("JobRow", row)


def row_for_key(db: sqlite3.Connection, key: str) -> JobRow:
    """Fetch and bind one indexed listing leaf to its canonical job row."""
    return _row_for_id(db, key, _reverse(key[-64:]))


def row_for_id_key(db: sqlite3.Connection, key: str) -> JobRow:
    """Bind an enumerated ID-view leaf to its canonical row."""
    if not key.startswith(ID_SCOPE) or len(key) != KEY_LENGTH:
        raise ValueError("invalid queue ID proof key")
    return _row_for_id(db, key, key[SCOPE_LENGTH : SCOPE_LENGTH + 64])


def all_id_keys(db: sqlite3.Connection, anchored_root: str) -> Iterator[str]:
    """Enumerate the authenticated ID view for recovery snapshots."""
    after: str | None = None
    while True:
        batch = page_keys(db, ID_SCOPE, anchored_root, after=after, limit=100)
        if not batch:
            return
        yield from batch
        after = batch[-1]
