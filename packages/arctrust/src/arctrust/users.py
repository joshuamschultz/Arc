"""Human user identities for a deployment (SPEC-057 REQ-040/041/043).

A bearer token proves someone holds a secret. It does not say who they are, and
an audit trail whose approvals read "the operator token" cannot answer the only
question that matters after an incident: which person allowed this. So a
deployment has *users* — an email, a password, and a DID of their own — and the
authenticated user becomes the identity recorded against what they approve.

The local file is a cache of authority state persisted by
``arctrust.sealed_state.SealedAuthorityState``: an independent monotonic anchor
authenticates each revision and carries sealed recovery intent; private signing
keys stay in external custody.

Passwords are hashed with Argon2id via PyNaCl at interactive limits — Arc's
smallest target is a 4GB box, and sensitive limits would make a login there slow
enough that operators disable it.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate, ParamSpec, Protocol, TypeVar, cast

from nacl import pwhash
from nacl.exceptions import InvalidkeyError

from arctrust.audit import AuditSink, DurableAuditSink
from arctrust.byte_cipher import ByteCipher
from arctrust.identity import did_from_public_key, did_matches_pubkey, parse_did
from arctrust.monotonic import MonotonicAnchor
from arctrust.paths import users_file
from arctrust.sealed_state import (
    AuthorityCommitUncertainError,
    SealedAuthorityState,
    UserStoreError,
)

# Mention-safe: starts alphanumeric, no spaces, no dots (which would collide
# with sentence punctuation when an agent writes "@josh.").
_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}$")

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
    initial_claim_digest: str | None = None

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
        self._state = SealedAuthorityState(
            self.path,
            anchor=anchor,
            cipher=cipher,
            audit_sink=audit_sink,
            strict_audit_sink=strict_audit_sink,
            record_name="users",
            actor_did=actor_did,
        )
        self._users: dict[str, User] = {}
        self._mutex = threading.RLock()
        self._load()

    # --- persistence ----------------------------------------------------

    @_synchronized
    def _load(self) -> None:
        users: dict[str, User] = {}
        self._state.load(validate=lambda record: users.update(_users_from_record(record)))
        self._users = users

    @_synchronized
    def _save(self, candidate: dict[str, User]) -> None:
        try:
            self._state.save({"users": [asdict(u) for u in candidate.values()]})
        except AuthorityCommitUncertainError:
            # Anchored and written: the candidate is the authority now.
            self._users = candidate
            raise
        self._users = candidate

    def _check_write_head(self) -> None:
        self._state.assert_current()

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
        return self._add_new(
            email,
            password,
            handle=handle,
            display_name=display_name,
            roles=roles,
            pairings=pairings,
            org=org,
            initial_claim_digest=None,
        )

    @_synchronized
    def claim_first_operator(
        self, email: str, password: str, *, org: str, claim_digest: str
    ) -> User:
        """Bind the first operator to the exact reserved hosted grant."""
        self._load()
        if self._users or not re.fullmatch(r"[0-9a-f]{64}", claim_digest):
            raise UserStoreError("first operator claim is unavailable")
        return self._add_new(
            email,
            password,
            roles=(OPERATOR,),
            org=org,
            initial_claim_digest=claim_digest,
        )

    def _add_new(
        self,
        email: str,
        password: str,
        *,
        handle: str | None = None,
        display_name: str = "",
        roles: tuple[str, ...] = (VIEWER,),
        pairings: dict[str, str] | None = None,
        org: str = "arc",
        initial_claim_digest: str | None,
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
            initial_claim_digest=initial_claim_digest,
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
        self,
        email: str,
        *,
        display_name: str | None = None,
        handle: str | None = None,
        pairings: dict[str, str | None] | None = None,
    ) -> User:
        """Validate and persist a profile edit as one authority revision."""
        self._check_write_head()
        user = self._require(email)
        resolved_handle = (
            self._resolve_handle(handle, user.email, taken_by=user.email, strict=True)
            if handle is not None
            else user.handle
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


def _users_from_record(raw: dict[str, Any]) -> dict[str, User]:
    """Decode and check every user in an anchored record; refuse the whole record otherwise."""
    try:
        records = raw["users"]
        if not isinstance(records, list):
            raise TypeError("user authority state must contain users")
        users = {str(u["email"]): _user_from_dict(u) for u in records}
        if len(users) != len(records):
            raise ValueError("duplicate user identity")
    except (TypeError, KeyError, ValueError, IndexError) as exc:
        raise UserStoreError("malformed user authority record") from exc
    return users


def _user_from_dict(record: dict[str, Any]) -> User:
    user = User(
        **{
            **record,
            "roles": tuple(record.get("roles", (VIEWER,))),
            "pairings": dict(record.get("pairings", {})),
        }
    )
    public_key = bytes.fromhex(user.public_key)
    if (
        len(public_key) != 32
        or not did_matches_pubkey(user.did, public_key)
        or parse_did(user.did)["agent_type"] != "user"
    ):
        raise ValueError("invalid user identity")
    if any(role not in ROLES for role in user.roles):
        raise ValueError("invalid user role")
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
