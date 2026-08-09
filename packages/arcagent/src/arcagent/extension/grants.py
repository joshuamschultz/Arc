"""Connections and grants — the deployment's one answer to "who can reach what".

A **connection** is a connected account defined once for the whole deployment: one
extension, one name, one credential, one host setup. A **grant** is
``(connection -> agent)``. An agent holding a grant gets that connection's verbs;
an agent without one gets nothing.

**Why this is not per-agent state.** ADR-029 puts an agent's own state — memory,
sessions, ``context.md``, identity, the audit chain — in the agent's workspace,
and this file deliberately sits outside it. A grant is not something the agent
knows about itself; it is the deployment's policy *about* the agent, and the
subject of a policy may not be its custodian. Keeping it in the agent directory is
what made possession of an ``[extensions.*]`` block indistinguishable from
permission to use it: any blueprint, scaffold, or agent-adjacent writer that
touched that directory silently handed over a live credential. Deny-by-default is
only a real property when the authority lives somewhere the subject does not
write, so this file lives beside the other fleet-wide files under ``arc_home()``
and is owner-only.

It also makes the operator's question answerable. "Who can read my mail" is one
read of one file, for every agent at once — a question no per-agent arrangement
can answer without walking the fleet and trusting that the walk was complete.

**Agent names are coordinates, not labels.** The same rule that governs a
connection name governs a grant, because the grant is matched against the agent's
directory name at attach time. A name this refuses could never match anything, so
refusing it where it is typed is the difference between a clear error and a grant
that is written, listed, and silently effective for no one.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from arcagent.core.errors import ExtensionError
from arcagent.extension.coordinates import is_coordinate
from arcagent.extension.coordinates import refusal as coordinate_refusal
from arcagent.utils.toml_writer import dumps_toml

#: The deployment file holding every connection and every grant, under ``arc_home()``.
CONNECTIONS_FILENAME = "connections.toml"

#: The table connections live in.
CONNECTIONS_TABLE = "connections"

#: Refusal code for a verb naming a connection this deployment has not defined.
#: A surface distinguishing "not found" from "refused" — a route answering 404
#: rather than 400 — branches on this rather than matching the message text.
NO_SUCH_CONNECTION = "CONNECTION_NOT_DEFINED"

#: Refusal code for a connections file that will not parse or does not have the
#: shape this module writes.
UNREADABLE_REGISTRY = "CONNECTION_REGISTRY_UNREADABLE"

#: Refusal code for a name — a connection's or an agent's — that cannot address
#: anything.
BAD_NAME = "CONNECTION_NAME_INVALID"


def _refuse(code: str, message: str, **details: Any) -> ExtensionError:
    """The one refusal shape every surface renders: a code to branch on, a line to show."""
    return ExtensionError(code=code, message=message, details=details)


class Connection(BaseModel):
    """One connected account, and every agent permitted to use it.

    ``extra="forbid"`` so a hand-edited key is a loud error rather than a setting
    the operator believes is in force and which nothing reads.

    ``agents`` is the grant list and the ONLY thing that decides access. It is
    ordered and de-duplicated on write so the file reads the same after a grant
    as it does after the same grant repeated, and a diff of this file is a diff of
    who can reach what.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    extension: str
    approval: str = "outbound"
    agents: tuple[str, ...] = ()

    def with_agents(self, agents: Sequence[str]) -> Connection:
        """This connection, granted to exactly ``agents``."""
        return self.model_copy(update={"agents": tuple(agents)})


class ConnectionRegistry:
    """The deployment's connections and grants, read from and written to one file.

    A missing file is an empty deployment, never an error: a machine that has
    connected nothing is the ordinary starting state, and the file appears the
    first time something is connected.

    Every mutation rewrites the whole file at ``0600`` inside a ``0700``
    directory. This file decides which agent reaches which account, so a mode that
    let another user rewrite it would let them grant themselves one.
    """

    def __init__(self, arc_dir: Path | str) -> None:
        self._arc_dir = Path(arc_dir).expanduser()

    @property
    def path(self) -> Path:
        """Where the deployment's connections live — what a surface tells an operator."""
        return self._arc_dir / CONNECTIONS_FILENAME

    # --- reading ---------------------------------------------------------

    def all(self) -> dict[str, Connection]:
        """Every connection this deployment has defined, by name."""
        document = self._read()
        table = document.get(CONNECTIONS_TABLE, {})
        if not isinstance(table, dict):
            raise _refuse(
                UNREADABLE_REGISTRY, f"[{CONNECTIONS_TABLE}] in {self.path} is not a table"
            )
        connections: dict[str, Connection] = {}
        for name, block in table.items():
            try:
                connections[name] = Connection.model_validate(block)
            except ValidationError as exc:
                raise _refuse(
                    UNREADABLE_REGISTRY,
                    f"[{CONNECTIONS_TABLE}.{name}] in {self.path} is invalid — {exc}",
                    connection=name,
                ) from exc
        return connections

    def get(self, name: str) -> Connection:
        """One connection, or refuse naming it.

        Raises:
            ExtensionError: Nothing is defined under that name
                (``code`` :data:`NO_SUCH_CONNECTION`).
        """
        found = self.all().get(name)
        if found is None:
            raise _refuse(
                NO_SUCH_CONNECTION,
                f"this deployment has no connection named {name!r} — "
                f"connect it first, then grant it",
                connection=name,
            )
        return found

    def granted_to(self, agent: str) -> dict[str, Connection]:
        """Every connection this agent may use. **The enforcement read.**

        Deny by default falls out of the shape rather than out of a check: an
        agent nobody has named appears in no grant list, so this answers empty and
        the agent attaches nothing. There is no state an agent could be in where
        omitting a check would grant it something.
        """
        return {
            name: connection
            for name, connection in self.all().items()
            if agent in connection.agents
        }

    # --- writing ---------------------------------------------------------

    def define(self, name: str, connection: Connection) -> Connection:
        """Add or replace one connection, leaving every other one alone."""
        _check_name("connection name", name)
        stored = connection.with_agents(_ordered(connection.agents))
        document = self._read()
        table = document.setdefault(CONNECTIONS_TABLE, {})
        if not isinstance(table, dict):
            raise _refuse(
                UNREADABLE_REGISTRY, f"[{CONNECTIONS_TABLE}] in {self.path} is not a table"
            )
        table[name] = stored.model_dump(mode="json")
        self._write(document)
        return stored

    def forget(self, name: str) -> bool:
        """Drop one connection and every grant on it. False when there was none."""
        document = self._read()
        table = document.get(CONNECTIONS_TABLE)
        if not isinstance(table, dict) or name not in table:
            return False
        table.pop(name)
        if not table:
            document.pop(CONNECTIONS_TABLE)
        self._write(document)
        return True

    def grant(self, name: str, agents: Sequence[str]) -> Connection:
        """Permit ``agents`` to use one connection, keeping the grants already held.

        Raises:
            ExtensionError: The connection is not defined
                (``code`` :data:`NO_SUCH_CONNECTION`), or an agent name cannot
                address anything (``code`` :data:`BAD_NAME`).
        """
        current = self.get(name)
        for agent in agents:
            _check_name("agent name", agent)
        return self.define(name, current.with_agents(_ordered([*current.agents, *agents])))

    def revoke(self, name: str, agents: Sequence[str]) -> Connection:
        """Take one connection back from ``agents``. The account itself is untouched.

        Revoking from an agent that never held it is not an error — an operator
        making sure nobody has something must not be stopped by a name that
        already does not.
        """
        current = self.get(name)
        dropped = set(agents)
        return self.define(
            name, current.with_agents([held for held in current.agents if held not in dropped])
        )

    # --- internals -------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        """Parse the deployment file, or answer empty when there is not one yet."""
        if not self.path.is_file():
            return {}
        try:
            parsed: dict[str, Any] = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise _refuse(UNREADABLE_REGISTRY, f"{self.path} — {exc}") from exc
        return parsed

    def _write(self, document: dict[str, Any]) -> None:
        """Replace the file atomically, owner-only from creation."""
        self._arc_dir.mkdir(parents=True, exist_ok=True)
        temp = self._arc_dir / f".{CONNECTIONS_FILENAME}.tmp.{os.getpid()}.{os.urandom(6).hex()}"
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, dumps_toml(document).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(str(temp), str(self.path))
        except OSError:
            temp.unlink(missing_ok=True)
            raise


def _check_name(label: str, value: str) -> None:
    """Refuse a name that could never address anything, where it was typed."""
    if not is_coordinate(value):
        raise _refuse(BAD_NAME, coordinate_refusal(label, value), name=value)


def _ordered(agents: Sequence[str]) -> tuple[str, ...]:
    """The grant list, de-duplicated, in the order the grants were made."""
    seen: dict[str, None] = {}
    for agent in agents:
        seen.setdefault(agent, None)
    return tuple(seen)


__all__ = [
    "BAD_NAME",
    "CONNECTIONS_FILENAME",
    "CONNECTIONS_TABLE",
    "NO_SUCH_CONNECTION",
    "UNREADABLE_REGISTRY",
    "Connection",
    "ConnectionRegistry",
]
