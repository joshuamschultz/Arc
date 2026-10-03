"""A real ``UserStore`` over in-memory authority fakes, for tests that need accounts.

Only the custody edges are faked (key issuer, monotonic anchor, at-rest cipher);
hashing, validation, roles and persistence are the production store's own.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

from arctrust import AnchorHead, NullSink, UserStore
from nacl.signing import SigningKey


class FakeIssuer:
    def __init__(self) -> None:
        self.keys: dict[str, SigningKey] = {}

    def create_user_key(self, ref: str) -> bytes:
        self.keys[ref] = SigningKey.generate()
        return self.public_key(ref)

    def public_key(self, ref: str) -> bytes:
        return bytes(self.keys[ref].verify_key)


class FakeAnchor:
    scope = "test/accounts"

    def __init__(self) -> None:
        self.head: AnchorHead | None = None

    def latest(self) -> AnchorHead | None:
        return self.head

    def compare_and_advance(
        self, expected: AnchorHead | None, digest: str, intent: str
    ) -> AnchorHead:
        if expected != self.head:
            raise RuntimeError("stale")
        self.head = AnchorHead(
            scope=self.scope,
            version=1 if expected is None else expected.version + 1,
            digest=digest,
            previous_digest=None if expected is None else expected.digest,
            intent=intent,
        )
        return self.head


class FakeCipher:
    def seal(self, payload: bytes) -> str:
        return base64.b64encode(payload).decode()

    def open(self, sealed: str) -> bytes:
        return base64.b64decode(sealed)


def user_store_factory(state_dir: Path) -> Callable[[], UserStore]:
    """A factory that reopens one store under ``state_dir`` on every call."""
    path = state_dir / "users.json"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    opts = {
        "issuer": FakeIssuer(),
        "anchor": FakeAnchor(),
        "cipher": FakeCipher(),
        "audit_sink": NullSink(),
        "actor_did": "did:arc:test:user/audit",
    }

    def factory() -> UserStore:
        return UserStore(path, **opts)  # type: ignore[arg-type]  # reason: fakes satisfy the protocols

    return factory
