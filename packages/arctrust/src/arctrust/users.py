"""Human user identities for a deployment (SPEC-057 REQ-040/041/043).

A bearer token proves someone holds a secret. It does not say who they are, and
an audit trail whose approvals read "the operator token" cannot answer the only
question that matters after an incident: which person allowed this. So a
deployment has *users* — an email, a password, and a DID of their own — and the
authenticated user becomes the identity recorded against what they approve.

Storage mirrors :mod:`arctrust.operator`: one 0600 file under a 0700 directory in
``arc_home``, written with direct filesystem I/O. It holds password hashes and
each user's signing seed, so it carries exactly the same custody expectations as
``operator.key`` and is checked the same way at load.

Passwords are hashed with Argon2id via PyNaCl at interactive limits — Arc's
smallest target is a 4GB box, and sensitive limits would make a login there slow
enough that operators disable it.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from nacl import pwhash
from nacl.exceptions import InvalidkeyError

from arctrust.identity import did_from_public_key
from arctrust.keypair import generate_keypair
from arctrust.paths import arc_home

# Mention-safe: starts alphanumeric, no spaces, no dots (which would collide
# with sentence punctuation when an agent writes "@josh.").
_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}$")

_FILE_MODE = 0o600
_DIR_MODE = 0o700

VIEWER = "viewer"
OPERATOR = "operator"
ROLES = (VIEWER, OPERATOR)


class UserStoreError(RuntimeError):
    """The user store is unreadable, mis-permissioned, or malformed."""


def default_users_path() -> Path:
    """``<arc_home>/users.json`` — one store per deployment."""
    return arc_home() / "users.json"


@dataclass(frozen=True)
class User:
    id: str
    email: str
    password_hash: str
    did: str
    public_key: str
    signing_seed: str
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

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_users_path()
        self._users: dict[str, User] = {}
        if self.path.exists():
            self._load()

    # --- persistence ----------------------------------------------------

    def _load(self) -> None:
        _reject_insecure(self.path)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UserStoreError(f"cannot read {self.path}: {exc}") from exc
        try:
            self._users = {
                str(u["email"]): User(
                    **{
                        **u,
                        "roles": tuple(u.get("roles", (VIEWER,))),
                        "pairings": dict(u.get("pairings", {})),
                    }
                )
                for u in raw.get("users", [])
            }
        except (TypeError, KeyError) as exc:
            raise UserStoreError(f"malformed user record in {self.path}: {exc}") from exc

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(_DIR_MODE)
        payload = {"users": [asdict(u) for u in self._users.values()]}
        # Write-then-rename so a crash mid-write cannot leave a truncated store
        # that locks everyone out of their own deployment.
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.chmod(_FILE_MODE)
        tmp.replace(self.path)

    # --- queries --------------------------------------------------------

    def list(self) -> list[User]:
        return sorted(self._users.values(), key=lambda u: u.email)

    def get(self, email: str) -> User | None:
        return self._users.get(_normalize(email))

    def is_empty(self) -> bool:
        return not self._users

    def by_pairing(self, platform: str, external_id: str) -> User | None:
        """Find the person a surface identity belongs to.

        The caller supplies both halves; this store never guesses which platform
        a bare id came from.
        """
        for user in self._users.values():
            if user.pairings.get(platform) == external_id:
                return user
        return None

    # --- mutations ------------------------------------------------------

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
        email = _normalize(email)
        if email in self._users:
            raise ValueError(f"user already exists: {email}")
        for role in roles:
            if role not in ROLES:
                raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")

        resolved = self._resolve_handle(handle, email, taken_by=None)
        kp = generate_keypair()
        user = User(
            id=str(uuid.uuid4()),
            email=email,
            password_hash=hash_password(password),
            did=did_from_public_key(kp.public_key, org=org, agent_type="user"),
            public_key=kp.public_key.hex(),
            signing_seed=kp.private_key.hex(),
            handle=resolved,
            display_name=display_name.strip(),
            roles=tuple(roles),
            pairings=dict(pairings or {}),
        )
        self._users[email] = user
        self.save()
        return user

    def set_handle(self, email: str, handle: str) -> User:
        user = self._require(email)
        resolved = self._resolve_handle(handle, user.email, taken_by=user.email, strict=True)
        updated = replace(user, handle=resolved)
        self._users[updated.email] = updated
        self.save()
        return updated

    def set_display_name(self, email: str, display_name: str) -> User:
        user = self._require(email)
        updated = replace(user, display_name=display_name.strip())
        self._users[updated.email] = updated
        self.save()
        return updated

    def by_handle(self, handle: str) -> User | None:
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

    def set_password(self, email: str, password: str) -> User:
        user = self._require(email)
        updated = replace(user, password_hash=hash_password(password))
        self._users[updated.email] = updated
        self.save()
        return updated

    def set_roles(self, email: str, roles: tuple[str, ...]) -> User:
        for role in roles:
            if role not in ROLES:
                raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
        user = self._require(email)
        updated = replace(user, roles=tuple(roles))
        self._users[updated.email] = updated
        self.save()
        return updated

    def set_pairing(self, email: str, platform: str, external_id: str | None) -> User:
        """Attach or drop one external identity. ``None`` unpairs.

        A platform is refused if another account already claims that id on it:
        two people sharing one surface identity makes every message from it
        ambiguous about who sent it.
        """
        platform = platform.strip().lower()
        if not platform:
            raise ValueError("platform is required")
        user = self._require(email)
        if external_id:
            owner = self.by_pairing(platform, external_id)
            if owner is not None and owner.email != user.email:
                raise ValueError(
                    f"{platform} id {external_id} is already paired to {owner.email}"
                )
        merged = dict(user.pairings)
        if external_id:
            merged[platform] = external_id
        else:
            merged.pop(platform, None)
        updated = replace(user, pairings=merged)
        self._users[updated.email] = updated
        self.save()
        return updated

    def set_disabled(self, email: str, disabled: bool) -> User:
        user = self._require(email)
        updated = replace(user, disabled=disabled)
        self._users[updated.email] = updated
        self.save()
        return updated

    def remove(self, email: str) -> None:
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
        del self._users[email]
        self.save()

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


def _reject_insecure(path: Path) -> None:
    """Fail closed when the store is group/other readable or a symlink."""
    if path.is_symlink():
        raise UserStoreError(f"user store is a symlink, refusing to read it: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise UserStoreError(
            f"user store has insecure permissions {oct(mode)}; expected 0o600: {path}"
        )
    if path.stat().st_uid != os.getuid():
        raise UserStoreError(f"user store is not owned by this user: {path}")


# Hashed once at import so an unknown email costs the same as a known one.
_DUMMY_HASH = pwhash.argon2id.str(b"arc-timing-equalizer").decode()

__all__ = [
    "OPERATOR",
    "ROLES",
    "VIEWER",
    "User",
    "UserStore",
    "UserStoreError",
    "default_users_path",
    "hash_password",
]
