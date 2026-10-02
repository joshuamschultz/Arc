"""Read a connection's sealed credential back out of the test deployment's custody.

Credentials no longer touch a file. These helpers open the same arcstore rows the
routes wrote, with the same deployment cipher, so a test can assert the value is
held, opens, and appears nowhere in the stored row.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from arcagent.core.tier import Tier
from arcagent.extension.custody import CREDENTIAL_COLLECTION, CredentialRowStore
from arcagent.extension.custody_select import deployment_cipher
from arcstore.backends.memory import FakeBackend


def new_backend() -> FakeBackend:
    """The ArcStore fake a test app carries as ``app.state.arcstore_backend``."""
    return FakeBackend()


def _store(backend: Any, arc_dir: Path) -> CredentialRowStore:
    return CredentialRowStore(backend, deployment_cipher(arc_dir, tier=Tier.PERSONAL))


def custody_field(backend: Any, arc_dir: Path, connection: str, field: str) -> str | None:
    """The opened value of one credential field, or ``None`` when no row holds it."""
    store = _store(backend, arc_dir)
    row = asyncio.run(store.read(connection))
    if row is None:
        return None
    secret = store.open_field(row, field)
    return None if secret is None else secret.reveal()


def put_custody_field(
    backend: Any, arc_dir: Path, connection: str, field: str, value: str
) -> None:
    """Seal a value into custody directly, the way a hand-edited or older row would be."""
    asyncio.run(
        _store(backend, arc_dir).put_fields(
            connection, {field: value}, actor_did="did:arc:test:operator"
        )
    )


def has_custody_row(backend: Any, arc_dir: Path, connection: str) -> bool:
    """Whether a sealed row exists for the connection."""
    return asyncio.run(_store(backend, arc_dir).read(connection)) is not None


def raw_custody_rows(backend: Any) -> str:
    """Every custody row exactly as stored, as one string, for plaintext sweeps."""
    rows = asyncio.run(backend.mutable_query(CREDENTIAL_COLLECTION))
    return json.dumps(rows, default=str)
