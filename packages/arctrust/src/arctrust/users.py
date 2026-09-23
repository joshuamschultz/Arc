"""Human user identities for a deployment (SPEC-057 REQ-040/041/043).

A bearer token proves someone holds a secret. It does not say who they are, and
an audit trail whose approvals read "the operator token" cannot answer the only
question that matters after an incident: which person allowed this. So a
deployment has *users* — an email, a password, and a DID of their own — and the
authenticated user becomes the identity recorded against what they approve.

The local file is a cache of authority state. An independent monotonic anchor
authenticates each revision and carries sealed recovery intent; private signing
keys stay in external custody.

Passwords are hashed with Argon2id via PyNaCl at interactive limits — Arc's
smallest target is a 4GB box, and sensitive limits would make a login there slow
enough that operators disable it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from functools import wraps
from pathlib import Path
from typing import Concatenate, ParamSpec, Protocol, TypeVar, cast

from nacl import pwhash
from nacl.exceptions import InvalidkeyError

from arctrust.audit import AuditEvent, AuditSink, DurableAuditSink
from arctrust.byte_cipher import ByteCipher
from arctrust.identity import did_from_public_key, did_matches_pubkey, parse_did
from arctrust.monotonic import AnchorHead, MonotonicAnchor
from arctrust.paths import users_file

# Mention-safe: starts alphanumeric, no spaces, no dots (which would collide
# with sentence punctuation when an agent writes "@josh.").
_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}$")

_FILE_MODE = 0o600
_DIR_MODE = 0o700
_LOCK_TIMEOUT_SECONDS = 5

VIEWER = "viewer"
OPERATOR = "operator"
ROLES = (VIEWER, OPERATOR)

_P = ParamSpec("_P")
_T = TypeVar("_T")


def _synchronized(
    method: Callable[Concatenate[UserStore, _P], _T],
) -> Callable[Concatenate[UserStore, _P], _T]:
    @wraps(method)
    def wrapped(self: UserStore, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        if not self._mutex.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            raise UserStoreError("user authority instance is busy")
        try:
            return method(self, *args, **kwargs)
        finally:
            self._mutex.release()

    return cast("Callable[Concatenate[UserStore, _P], _T]", wrapped)


class UserStoreError(RuntimeError):
    """The user store is unreadable, mis-permissioned, or malformed."""


class UserKeyIssuer(Protocol):
    """Creates nonexportable user signing keys in independent custody."""

    def create_user_key(self, key_ref: str) -> bytes: ...

    def public_key(self, key_ref: str) -> bytes: ...


def default_users_path() -> Path:
    """``<arc_state>/users.json`` — one store per deployment."""
    return users_file()


@dataclass(frozen=True)
class User:
    id: str
    email: str
    password_hash: str
    did: str
    public_key: str
    signing_key_ref: str
    signing_algorithm: str = "ed25519"
    # How an agent addresses this person: "@josh" in a message, ``user://josh``
    # as an address. Unique across the deployment, because a mention that could
    # mean two people is a message that reaches neither reliably.
    handle: str = ""
    # What an agent calls them in prose. Free text; no uniqueness.
    display_name: str = ""
    roles: tuple[str, ...] = (VIEWER,)
    # Paired external surface identities (REQ-041): ``{platform: external_id}``.
    # The platform key is an opaque string owned by whichever gateway package
    # provides that surface. arctrust deliberately does not know what any of
    # them mean — it is the trust leaf, and a hardcoded platform here would put
    # knowledge of a chat product inside the cryptographic foundation.
    pairings: dict[str, str] = field(default_factory=dict)
    disabled: bool = False
    created_at: float = field(default_factory=time.time)

    @property
    def is_operator(self) -> bool:
        return OPERATOR in self.roles

    @property
    def called(self) -> str:
        """What to call this person in prose, falling back sensibly."""
        return self.display_name or self.handle or self.email

    def redacted(self) -> dict[str, object]:
        """The shape safe to hand to an API response or a terminal."""
        return {
            "id": self.id,
            "email": self.email,
            "did": self.did,
            "handle": self.handle,
            "display_name": self.display_name,
            "roles": list(self.roles),
            "pairings": dict(self.pairings),
            "disabled": self.disabled,
            "created_at": self.created_at,
        }


def hash_password(password: str) -> str:
    _reject_weak(password)
    return pwhash.argon2id.str(password.encode()).decode()


def _reject_weak(password: str) -> None:
    # Deliberately a floor, not a character-class policy: length is the only
    # rule that reliably correlates with strength, and the rest trains people
    # into Password1! variants.
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")


class UserStore:
    """Load/save the deployment's users. Every mutation writes through."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        issuer: UserKeyIssuer,
        anchor: MonotonicAnchor,
        cipher: ByteCipher,
        audit_sink: AuditSink,
        actor_did: str,
        strict_audit_sink: DurableAuditSink | None = None,
    ) -> None:
        self.path = path or default_users_path()
        self._issuer = issuer
        self._anchor = anchor
        self._cipher = cipher
        self._audit_sink = audit_sink
        self._strict_audit_sink = strict_audit_sink
        self._actor_did = actor_did
        self._users: dict[str, User] = {}
        self._head: AnchorHead | None = None
        self._mutex = threading.RLock()
        self._dir_fd: int | None = None
        self._lock_fd: int | None = None
        self._load()

    # --- persistence ----------------------------------------------------

    @_synchronized
    def _load(self) -> None:
        with self._lock():
            self._load_locked()

    def _load_locked(self) -> None:
        head = self._anchor.latest()
        if head is None:
            if self._read_file() is not None or self._head is not None:
                raise UserStoreError("user authority anchor missing")
            return
        if head.scope != self._anchor.scope:
            raise UserStoreError("user authority anchor scope mismatch")
        sealed = self._read_file()
        if sealed is None:
            self._recover(head)
            sealed = self._read_file()
        try:
            if sealed is None:
                raise UserStoreError("user authority recovery failed")
            if hashlib.sha256(sealed).hexdigest() != head.digest:
                self._recover(head)
                sealed = self._read_file()
            if sealed is None:
                raise UserStoreError("user authority recovery failed")
            if hashlib.sha256(sealed).hexdigest() != head.digest:
                raise UserStoreError("user authority integrity mismatch")
            raw = json.loads(self._cipher.open(sealed.decode("ascii")))
        except (OSError, json.JSONDecodeError) as exc:
            raise UserStoreError("cannot read user authority state") from exc
        try:
            if not isinstance(raw, dict) or not isinstance(raw.get("users"), list):
                raise TypeError("user authority state must contain users")
            users = {
                str(u["email"]): User(
                    **{
                        **u,
                        "roles": tuple(u.get("roles", (VIEWER,))),
                        "pairings": dict(u.get("pairings", {})),
                    }
                )
                for u in raw.get("users", [])
            }
            if len(users) != len(raw["users"]):
                raise ValueError("duplicate user identity")
            for user in users.values():
                public_key = bytes.fromhex(user.public_key)
                if (
                    len(public_key) != 32
                    or not did_matches_pubkey(user.did, public_key)
                    or parse_did(user.did)["agent_type"] != "user"
                ):
                    raise ValueError("invalid user identity")
                if any(role not in ROLES for role in user.roles):
                    raise ValueError("invalid user role")
            self._audit("users.read", "allow", head.digest)
            self._users = users
            self._head = head
        except (TypeError, KeyError, ValueError, IndexError) as exc:
            raise UserStoreError("malformed user authority record") from exc

    def _recover(self, head: AnchorHead) -> None:
        if not head.intent:
            raise UserStoreError("user authority recovery intent absent")
        sealed = head.intent.encode("ascii")
        if hashlib.sha256(sealed).hexdigest() != head.digest:
            raise UserStoreError("user authority recovery intent invalid")
        existing = self._read_file()
        if existing is not None:
            current = hashlib.sha256(existing).hexdigest()
            if current != head.previous_digest:
                raise UserStoreError("unexplained user authority rollback")
        self._write(sealed)

    @_synchronized
    def _save(self, candidate: dict[str, User]) -> None:
        with self._lock():
            self._save_locked(candidate)

    def _save_locked(self, candidate: dict[str, User]) -> None:
        payload = json.dumps(
            {"users": [asdict(u) for u in candidate.values()]},
            sort_keys=True, separators=(",", ":"),
        ).encode()
        sealed = self._cipher.seal(payload)
        digest = hashlib.sha256(sealed.encode("ascii")).hexdigest()
        self._audit("users.change", "attempt", digest)
        self._assert_pinned()
        head = self._anchor.compare_and_advance(self._head, digest, sealed)
        if self._anchor.latest() != head:
            raise UserStoreError("user authority advanced before local write")
        try:
            self._write(sealed.encode("ascii"))
        except OSError as exc:
            raise UserStoreError("user authority write outcome is uncertain") from exc
        self._head = head
        self._users = candidate
        try:
            self._audit("users.change", "allow", digest)
        except Exception as exc:
            raise UserStoreError("user authority committed; audit outcome is uncertain") from exc

    @contextmanager
    def _lock(self) -> Iterator[None]:
        directory = self.path.parent
        directory.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        if directory.resolve() != directory.absolute() or directory.is_symlink():
            raise UserStoreError("user authority directory is a symlink")
        metadata = directory.stat()
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise UserStoreError("user authority directory is not private")
        dir_fd = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            lock_fd = os.open(
                ".users.lock",
                os.O_RDWR | os.O_NONBLOCK | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                _FILE_MODE,
                dir_fd=dir_fd,
            )
            try:
                lock_meta = os.fstat(lock_fd)
                if (
                    lock_meta.st_uid != os.getuid()
                    or stat.S_IMODE(lock_meta.st_mode) != _FILE_MODE
                    or not stat.S_ISREG(lock_meta.st_mode)
                    or lock_meta.st_nlink != 1
                ):
                    raise UserStoreError("user authority lock is not private")
                deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
                while True:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise UserStoreError("user authority lock is busy") from None
                        time.sleep(0.02)
                try:
                    self._dir_fd = dir_fd
                    self._lock_fd = lock_fd
                    self._assert_pinned()
                    yield
                finally:
                    self._dir_fd = None
                    self._lock_fd = None
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        finally:
            os.close(dir_fd)

    def _check_write_head(self) -> None:
        if self._anchor.latest() != self._head:
            self._audit("users.change", "deny", self._head.digest if self._head else "0" * 64)
            raise UserStoreError("stale user authority writer")

    def _audit(self, action: str, outcome: str, digest: str) -> None:
        event = AuditEvent(
            actor_did=self._actor_did, action=action, target=self._anchor.scope,
            outcome=outcome, payload_hash=digest,
        )
        if action == "users.change" and self._strict_audit_sink is not None:
            self._strict_audit_sink.write_durable(event)
        else:
            self._audit_sink.write(event)

    def _assert_pinned(self) -> int:
        dir_fd, lock_fd = self._dir_fd, self._lock_fd
        if dir_fd is None or lock_fd is None:
            raise UserStoreError("user authority lock is absent")
        directory = self.path.parent
        if directory.resolve() != directory.absolute():
            raise UserStoreError("user authority directory changed")
        try:
            directory_meta = os.stat(directory, follow_symlinks=False)
            lock_meta = os.stat(".users.lock", dir_fd=dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise UserStoreError("user authority lock or directory changed") from exc
        pinned_meta = os.fstat(dir_fd)
        if (directory_meta.st_dev, directory_meta.st_ino) != (
            pinned_meta.st_dev, pinned_meta.st_ino
        ):
            raise UserStoreError("user authority directory changed")
        pinned_lock = os.fstat(lock_fd)
        if ((lock_meta.st_dev, lock_meta.st_ino) != (pinned_lock.st_dev, pinned_lock.st_ino)
                or pinned_lock.st_nlink != 1):
            raise UserStoreError("user authority lock changed")
        return dir_fd

    def _read_file(self) -> bytes | None:
        dir_fd = self._assert_pinned()
        try:
            fd = os.open(
                self.path.name,
                os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=dir_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise UserStoreError("user store is not a private regular file") from exc
        try:
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != _FILE_MODE):
                raise UserStoreError("user store has insecure permissions or file type")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                return stream.read()
        finally:
            os.close(fd)

    def _write(self, payload: bytes) -> None:
        dir_fd = self._assert_pinned()
        name = f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        tmp_fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            _FILE_MODE,
            dir_fd=dir_fd,
        )
        try:
            with os.fdopen(tmp_fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._assert_pinned()
            os.replace(name, self.path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.fsync(dir_fd)
        finally:
            try:
                os.unlink(name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass

    # --- queries --------------------------------------------------------

    @_synchronized
    def list(self) -> list[User]:
        self._load()
        return sorted(self._users.values(), key=lambda u: u.email)

    @_synchronized
    def get(self, email: str) -> User | None:
        self._load()
        return self._users.get(_normalize(email))

    @_synchronized
    def is_empty(self) -> bool:
        self._load()
        return not self._users

    @_synchronized
    def by_pairing(self, platform: str, external_id: str) -> User | None:
        """Find the person a surface identity belongs to.

        The caller supplies both halves; this store never guesses which platform
        a bare id came from.
        """
        self._load()
        for user in self._users.values():
            if user.pairings.get(platform) == external_id:
                return user
        return None

    # --- mutations ------------------------------------------------------

    @_synchronized
    def add(
        self,
        email: str,
        password: str,
        *,
        handle: str | None = None,
        display_name: str = "",
        roles: tuple[str, ...] = (VIEWER,),
        pairings: dict[str, str] | None = None,
        org: str = "arc",
    ) -> User:
        self._check_write_head()
        email = _normalize(email)
        if email in self._users:
            raise ValueError(f"user already exists: {email}")
        for role in roles:
            if role not in ROLES:
                raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")

        resolved = self._resolve_handle(handle, email, taken_by=None)
        password_hash = hash_password(password)
        key_ref = f"arc-user-{uuid.uuid4().hex}"
        public_key = self._issuer.create_user_key(key_ref)
        if public_key != self._issuer.public_key(key_ref) or len(public_key) != 32:
            raise UserStoreError("user key issuer returned an unverified key")
        user = User(
            id=str(uuid.uuid4()),
            email=email,
            password_hash=password_hash,
            did=did_from_public_key(public_key, org=org, agent_type="user"),
            public_key=public_key.hex(),
            signing_key_ref=key_ref,
            handle=resolved,
            display_name=display_name.strip(),
            roles=tuple(roles),
            pairings=dict(pairings or {}),
        )
        self._save({**self._users, email: user})
        return user

    @_synchronized
    def set_handle(self, email: str, handle: str) -> User:
        self._check_write_head()
        user = self._require(email)
        resolved = self._resolve_handle(handle, user.email, taken_by=user.email, strict=True)
        updated = replace(user, handle=resolved)
        self._save({**self._users, updated.email: updated})
        return updated

    @_synchronized
    def set_display_name(self, email: str, display_name: str) -> User:
        self._check_write_head()
        user = self._require(email)
        updated = replace(user, display_name=display_name.strip())
        self._save({**self._users, updated.email: updated})
        return updated

    @_synchronized
    def update_profile(
        self, email: str, *, display_name: str | None = None,
        handle: str | None = None, pairings: dict[str, str | None] | None = None,
    ) -> User:
        """Validate and persist a profile edit as one authority revision."""
        self._check_write_head()
        user = self._require(email)
        resolved_handle = (
            self._resolve_handle(handle, user.email, taken_by=user.email, strict=True)
            if handle is not None else user.handle
        )
        merged = dict(user.pairings)
        for raw_platform, external_id in (pairings or {}).items():
            platform = raw_platform.strip().lower()
            if not platform:
                raise ValueError("platform is required")
            if external_id:
                owner = self.by_pairing(platform, external_id)
                if owner is not None and owner.email != user.email:
                    raise ValueError(
                        f"{platform} id {external_id} is already paired to {owner.email}"
                    )
                merged[platform] = external_id
            else:
                merged.pop(platform, None)
        updated = replace(
            user,
            display_name=display_name.strip() if display_name is not None else user.display_name,
            handle=resolved_handle,
            pairings=merged,
        )
        self._save({**self._users, updated.email: updated})
        return updated

    @_synchronized
    def by_handle(self, handle: str) -> User | None:
        self._load()
        wanted = handle.lstrip("@").strip().lower()
        for user in self._users.values():
            if user.handle == wanted:
                return user
        return None

    def _resolve_handle(
        self, handle: str | None, email: str, *, taken_by: str | None, strict: bool = False
    ) -> str:
        """Validate an explicit handle, or derive a free one from the email.

        An explicitly requested handle that is taken is an error — silently
        handing someone ``josh2`` when they asked for ``josh`` means their
        mentions quietly go to the wrong person. A *derived* handle may be
        suffixed, because nobody asked for it.
        """
        if handle:
            candidate = handle.lstrip("@").strip().lower()
            if not _HANDLE_RE.match(candidate):
                raise ValueError(
                    "handle must be 2-31 characters: lowercase letters, digits, - or _, "
                    "starting with a letter or digit"
                )
            owner = self.by_handle(candidate)
            if owner is not None and owner.email != taken_by:
                raise ValueError(f"handle @{candidate} is already taken by {owner.email}")
            return candidate

        if strict:
            raise ValueError("handle cannot be empty")

        base = re.sub(r"[^a-z0-9_-]", "", email.split("@", 1)[0].lower()) or "user"
        base = base[:31].lstrip("-_") or "user"
        candidate = base
        suffix = 2
        while (owner := self.by_handle(candidate)) is not None and owner.email != taken_by:
            candidate = f"{base[:28]}{suffix}"
            suffix += 1
        return candidate

    @_synchronized
    def set_password(self, email: str, password: str) -> User:
        self._check_write_head()
        user = self._require(email)
        updated = replace(user, password_hash=hash_password(password))
        self._save({**self._users, updated.email: updated})
        return updated

    @_synchronized
    def set_roles(self, email: str, roles: tuple[str, ...]) -> User:
        self._check_write_head()
        for role in roles:
            if role not in ROLES:
                raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
        user = self._require(email)
        updated = replace(user, roles=tuple(roles))
        self._save({**self._users, updated.email: updated})
        return updated

    @_synchronized
    def set_pairing(self, email: str, platform: str, external_id: str | None) -> User:
        """Attach or drop one external identity. ``None`` unpairs.

        A platform is refused if another account already claims that id on it:
        two people sharing one surface identity makes every message from it
        ambiguous about who sent it.
        """
        self._check_write_head()
        platform = platform.strip().lower()
        if not platform:
            raise ValueError("platform is required")
        user = self._require(email)
        if external_id:
            owner = self.by_pairing(platform, external_id)
            if owner is not None and owner.email != user.email:
                raise ValueError(f"{platform} id {external_id} is already paired to {owner.email}")
        merged = dict(user.pairings)
        if external_id:
            merged[platform] = external_id
        else:
            merged.pop(platform, None)
        updated = replace(user, pairings=merged)
        self._save({**self._users, updated.email: updated})
        return updated

    @_synchronized
    def set_disabled(self, email: str, disabled: bool) -> User:
        self._check_write_head()
        user = self._require(email)
        updated = replace(user, disabled=disabled)
        self._save({**self._users, updated.email: updated})
        return updated

    @_synchronized
    def remove(self, email: str) -> None:
        self._check_write_head()
        email = _normalize(email)
        if email not in self._users:
            raise ValueError(f"no such user: {email}")
        # Removing the last operator would leave a deployment nobody can approve
        # anything in, recoverable only by hand-editing this file.
        remaining = [u for e, u in self._users.items() if e != email]
        if self._users[email].is_operator and not any(u.is_operator for u in remaining):
            raise ValueError(
                f"{email} is the only operator; promote another user before removing them"
            )
        candidate = dict(self._users)
        del candidate[email]
        self._save(candidate)

    def _require(self, email: str) -> User:
        user = self.get(email)
        if user is None:
            raise ValueError(f"no such user: {email}")
        return user

    # --- authentication -------------------------------------------------

    def verify(self, email: str, password: str) -> User | None:
        """Return the user when the password matches, else None.

        A disabled user and a wrong password are the same answer on purpose, and
        an unknown email still pays the hashing cost — otherwise response time
        alone reveals which addresses have accounts.
        """
        user = self.get(email)
        candidate = user.password_hash if user else _DUMMY_HASH
        try:
            pwhash.verify(candidate.encode(), password.encode())
        except InvalidkeyError:
            return None
        if user is None or user.disabled:
            return None
        return user


def _normalize(email: str) -> str:
    return email.strip().lower()


# Hashed once at import so an unknown email costs the same as a known one.
_DUMMY_HASH = pwhash.argon2id.str(b"arc-timing-equalizer").decode()

__all__ = [
    "OPERATOR",
    "ROLES",
    "VIEWER",
    "User",
    "UserKeyIssuer",
    "UserStore",
    "UserStoreError",
    "default_users_path",
    "hash_password",
]
