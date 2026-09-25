"""Ordered authenticated queue indexes backed by one external root."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
from collections.abc import Iterator
from typing import cast

KEY_LENGTH = 144
SCOPE_LENGTH = 64
JobRow = tuple[str, str, str, str, int, float, str]

_EMPTY: list[str] = [""] * (KEY_LENGTH + 1)
_EMPTY[KEY_LENGTH] = hashlib.sha256(b"arc.queue.empty-leaf.v3").hexdigest()
for _depth in range(KEY_LENGTH - 1, -1, -1):
    _EMPTY[_depth] = hashlib.sha256(
        b"arc.queue.node.v3\0" + bytes.fromhex(_EMPTY[_depth + 1]) * 16
    ).hexdigest()


def empty_leaf() -> str:
    """Return the canonical absent-row digest."""
    return _EMPTY[KEY_LENGTH]


def tenant_key(tenant_id: str) -> str:
    """Return the fixed-size tenant projection key."""
    return hashlib.sha256(b"arc.queue.tenant.v3\0" + tenant_id.encode()).hexdigest()


def owner_key(owner_id: str) -> str:
    """Return the fixed-size owner projection key."""
    return hashlib.sha256(b"arc.queue.owner.v3\0" + owner_id.encode()).hexdigest()


def scope_key(tenant: str, owner: str | None, state: str | None) -> str:
    """Name one of the four exact tenant/owner/state views."""
    body = json.dumps([tenant, owner, state], separators=(",", ":")).encode()
    return hashlib.sha256(b"arc.queue.scope.v3\0" + body).hexdigest()


ID_SCOPE = hashlib.sha256(b"arc.queue.id-scope.v3").hexdigest()
GLOBAL_SCOPE = hashlib.sha256(b"arc.queue.global-scope.v3").hexdigest()
CONTROL_KEY = hashlib.sha256(b"arc.queue.control.v3").hexdigest() + "0" * 80


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
    """Bind every supported listing scope and direct ID lookup to one row."""
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
    """Locate a row independently of its mutable sort and scope projections."""
    if len(row_id) != 64:
        raise ValueError("invalid queue row id")
    return ID_SCOPE + row_id + "0" * 16


def row_digest(key: str, row: JobRow) -> str:
    payload = json.dumps([key, row], separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(b"arc.queue.row.v3\0" + payload).hexdigest()


def control_digest(sealed: str) -> str:
    return hashlib.sha256(b"arc.queue.control-row.v3\0" + sealed.encode()).hexdigest()


def root(db: sqlite3.Connection) -> str:
    """Return the persisted tree root or the canonical empty root."""
    row = db.execute("SELECT digest FROM queue_nodes WHERE depth = 0 AND prefix = ''").fetchone()
    return row[0] if row else _EMPTY[0]


def _children(
    db: sqlite3.Connection,
    depth: int,
    parent: str,
    cache: dict[tuple[int, str], list[str]] | None = None,
) -> list[str]:
    identity = (depth, parent)
    if cache is not None and identity in cache:
        return cache[identity]
    rows = db.execute(
        "SELECT prefix, digest FROM queue_nodes WHERE depth = ? AND prefix >= ? AND prefix < ?",
        (depth, parent, parent + "g"),
    ).fetchall()
    children = [_EMPTY[depth]] * 16
    for prefix, digest in rows:
        if len(prefix) != depth or not prefix.startswith(parent):
            raise ValueError("invalid queue proof node")
        children[int(prefix[-1], 16)] = digest
    if cache is not None:
        cache[identity] = children
    return children


def _parent_digest(children: list[str]) -> str:
    return hashlib.sha256(
        b"arc.queue.node.v3\0" + b"".join(bytes.fromhex(item) for item in children)
    ).hexdigest()


def set_leaf(db: sqlite3.Connection, key: str, digest: str) -> None:
    """Update one leaf and every ancestor in the caller's transaction."""
    if len(key) != KEY_LENGTH:
        raise ValueError("invalid queue proof key")
    current = digest
    for depth in range(KEY_LENGTH, -1, -1):
        prefix = key[:depth]
        if depth != KEY_LENGTH:
            children = _children(db, depth + 1, prefix)
            children[int(key[depth], 16)] = current
            current = _parent_digest(children)
        if current == _EMPTY[depth]:
            db.execute("DELETE FROM queue_nodes WHERE depth = ? AND prefix = ?", (depth, prefix))
        else:
            db.execute(
                "INSERT INTO queue_nodes(depth, prefix, digest) VALUES (?, ?, ?) "
                "ON CONFLICT(depth, prefix) DO UPDATE SET digest = excluded.digest",
                (depth, prefix, current),
            )


def verify_point(db: sqlite3.Connection, key: str, digest: str, anchored_root: str) -> None:
    """Prove a present row, or prove an absent ID leaf, against the anchor."""
    if len(key) != KEY_LENGTH:
        raise ValueError("invalid queue proof key")
    current = digest
    stored = db.execute(
        "SELECT digest FROM queue_nodes WHERE depth = ? AND prefix = ?",
        (KEY_LENGTH, key),
    ).fetchone()
    if (stored[0] if stored else _EMPTY[KEY_LENGTH]) != digest:
        raise ValueError("queue row proof does not match stored leaf")
    for depth in range(KEY_LENGTH - 1, -1, -1):
        children = _children(db, depth + 1, key[:depth])
        children[int(key[depth], 16)] = current
        current = _parent_digest(children)
    if current != anchored_root:
        raise ValueError("queue row proof does not match anchor")


def _scope_root(
    db: sqlite3.Connection,
    scope: str,
    anchored_root: str,
    cache: dict[tuple[int, str], list[str]],
) -> str:
    expected = anchored_root
    for depth in range(SCOPE_LENGTH):
        children = _children(db, depth + 1, scope[:depth], cache)
        if _parent_digest(children) != expected:
            raise ValueError("queue scope proof does not match anchor")
        expected = children[int(scope[depth], 16)]
    return expected


def scope_digest(db: sqlite3.Connection, scope: str, anchored_root: str) -> str:
    """Return the authenticated revision of one exact listing scope."""
    return _scope_root(db, scope, anchored_root, {})


def page_keys(
    db: sqlite3.Connection,
    scope: str,
    anchored_root: str,
    *,
    after: str | None,
    limit: int,
) -> list[str]:
    """Prove the next ordered leaves without scanning unrelated rows."""
    if len(scope) != SCOPE_LENGTH or limit < 1:
        raise ValueError("invalid queue page proof")
    cache: dict[tuple[int, str], list[str]] = {}
    expected = _scope_root(db, scope, anchored_root, cache)
    found: list[str] = []

    def visit(prefix: str, digest: str) -> None:
        if len(found) >= limit or digest == _EMPTY[len(prefix)]:
            return
        if after is not None and prefix + "f" * (KEY_LENGTH - len(prefix)) <= after:
            return
        if len(prefix) == KEY_LENGTH:
            found.append(prefix)
            return
        children = _children(db, len(prefix) + 1, prefix, cache)
        if _parent_digest(children) != digest:
            raise ValueError("queue range proof does not match anchor")
        for nibble, child in enumerate(children):
            visit(prefix + format(nibble, "x"), child)
            if len(found) >= limit:
                break

    visit(scope, expected)
    return found


def row_for_key(db: sqlite3.Connection, key: str) -> JobRow:
    """Fetch and bind one indexed leaf to its canonical job row."""
    row_id = _reverse(key[-64:])
    row = db.execute(
        "SELECT id, tenant_key, owner_key, state, version, updated, sealed FROM jobs WHERE id = ?",
        (row_id,),
    ).fetchone()
    if row is None or key not in index_keys(row):
        raise ValueError("queue indexed row missing or mismatched")
    sealed = db.execute(
        "SELECT digest FROM queue_nodes WHERE depth = ? AND prefix = ?",
        (KEY_LENGTH, key),
    ).fetchone()
    if sealed is None or sealed[0] != row_digest(key, row):
        raise ValueError("queue indexed row digest mismatch")
    return cast("JobRow", row)


def row_for_id_key(db: sqlite3.Connection, key: str) -> JobRow:
    """Bind an enumerated ID-view leaf to its canonical row."""
    if not key.startswith(ID_SCOPE) or len(key) != KEY_LENGTH:
        raise ValueError("invalid queue ID proof key")
    row_id = key[SCOPE_LENGTH : SCOPE_LENGTH + 64]
    row = db.execute(
        "SELECT id, tenant_key, owner_key, state, version, updated, sealed FROM jobs WHERE id = ?",
        (row_id,),
    ).fetchone()
    if row is None or key != id_key(row[0]):
        raise ValueError("queue ID proof row missing")
    sealed = db.execute(
        "SELECT digest FROM queue_nodes WHERE depth = ? AND prefix = ?",
        (KEY_LENGTH, key),
    ).fetchone()
    if sealed is None or sealed[0] != row_digest(key, row):
        raise ValueError("queue ID proof row mismatch")
    return cast("JobRow", row)


def all_id_keys(db: sqlite3.Connection, anchored_root: str) -> Iterator[str]:
    """Enumerate the authenticated ID view for full startup reconciliation."""
    after: str | None = None
    while True:
        batch = page_keys(db, ID_SCOPE, anchored_root, after=after, limit=100)
        if not batch:
            return
        yield from batch
        after = batch[-1]
