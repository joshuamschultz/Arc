"""SPEC-064 T-003 — the provider API-key store behind every surface that sets one.

``arc init`` used to end by telling the operator to append a key to ``~/.arc/.env``
by hand: no verb, no store, and nothing another surface could call. This module is
that store, and it is shaped by one decision (D-583): **a key value is write-only.**

* There is no ``get``. Nothing in the connection surfaces reads a key back — the
  value is read from the process environment by ``arcllm`` when a call is made.
* :class:`KeyStatus` carries ``present``, never a value, a prefix, a length, or a
  hash. A surface that cannot display a key cannot leak one, so a web panel is as
  safe as a terminal table by construction rather than by care (LLM02, LLM07).

The allowlist is the second load-bearing property. ``set`` writes an environment
variable into a file the whole deployment sources, so an unchecked name is an
arbitrary-env-var write — ``PATH``, ``LD_PRELOAD``, anything. Only a variable some
packaged provider declares is accepted, and ``arcllm`` is the sole declarer (D-581).

The file itself is the same owner-only recipe connector credentials use
(:class:`~arcagent.extension.secrets.EnvFile`, D-582), so an operator sees one file
format at one permission, not two.

:class:`~arcagent.core.errors.ExtensionError` is re-exported because it is the
refusal every caller of this store has to catch: a surface driving key setup
reaches arcagent through this module and needs nothing else from it (D-587).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from arcllm import list_provider_keys
from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.paths import arc_home

from arcagent.core.errors import ExtensionError
from arcagent.extension.secrets import EnvFile


@dataclass(frozen=True)
class KeyStatus:
    """One provider's key coordinate and whether the store holds a value for it.

    Deliberately nothing else: adding a prefix or a length here would put a
    fragment of a credential on every surface that renders this record.
    """

    provider: str
    env_var: str
    required: bool
    present: bool


def default_env_file(arc_dir: Path | None = None) -> Path:
    """The file every surface writes provider keys to: ``<arc_home>/.env``.

    The one ``arc init`` creates and the one the deployment sources. Resolved here
    so the CLI and the web route cannot drift onto two different files — a key set
    in one and invisible in the other is indistinguishable from a key that failed
    to save. ``arc_dir`` is the CLI's ``--arc-dir`` override, so pointing one
    command at one deployment's world still goes through this resolver.
    """
    return (arc_dir or arc_home()) / ".env"


class KeyStore:
    """Set, forget, and report presence of provider API keys. Never read one back."""

    def __init__(self, env_file: Path, *, sink: AuditSink | None = None) -> None:
        self._file = EnvFile(env_file)
        self._sink = sink

    async def list(self, *, caller_did: str) -> tuple[KeyStatus, ...]:
        """Every provider arcllm packages, and whether this store holds its key."""
        entries = await self._file.read()
        statuses = tuple(
            KeyStatus(
                provider=key.provider,
                env_var=key.api_key_env,
                required=key.required,
                present=bool(entries.get(key.api_key_env)),
            )
            for key in list_provider_keys()
        )
        self._audit("provider_key.list", str(self._file.path), caller_did, "allow")
        return statuses

    async def set(self, env_var: str, value: str, *, caller_did: str) -> None:
        """Store a key. The value goes to the file and nowhere else — no log, no event."""
        self._validate(env_var, value)
        await self._file.put(env_var, value)
        self._audit("provider_key.write", env_var, caller_did, "allow")

    async def delete(self, env_var: str, *, caller_did: str) -> bool:
        """Forget a key. True when one was removed."""
        self._declared(env_var)
        removed = await self._file.delete(env_var)
        outcome = "allow" if removed else "not_found"
        self._audit("provider_key.delete", env_var, caller_did, outcome)
        return removed

    def _validate(self, env_var: str, value: str) -> None:
        """Refuse anything that could write a variable — or an entry — nobody asked for.

        Names only the coordinate. A refusal travels to a terminal, a log line, and
        an HTTP body, so the rejected material must never be part of it.
        """
        self._declared(env_var)
        if not value:
            raise ExtensionError(
                code="PROVIDER_KEY_EMPTY",
                message=f"refusing to store an empty value for {env_var}",
                details={"env_var": env_var},
            )
        if any(character in value for character in "\n\r\x00"):
            raise ExtensionError(
                code="PROVIDER_KEY_VALUE_INVALID",
                message=f"the value for {env_var} contains a line break or NUL and was refused",
                details={"env_var": env_var},
            )

    @staticmethod
    def _declared(env_var: str) -> None:
        """The allowlist: arcllm's packaged providers, and nothing beyond them."""
        if env_var not in {key.api_key_env for key in list_provider_keys()}:
            raise ExtensionError(
                code="PROVIDER_KEY_UNKNOWN",
                message=f"no packaged provider reads {env_var}; refusing to touch it",
                details={"env_var": env_var},
            )

    def _audit(self, action: str, target: str, caller_did: str, outcome: str) -> None:
        """Record who touched which coordinate, with what result — never the value."""
        if self._sink is None:
            return
        emit(
            AuditEvent(
                actor_did=caller_did,
                action=action,
                target=f"provider_key:{target}",
                outcome=outcome,
            ),
            self._sink,
        )


__all__ = ["ExtensionError", "KeyStatus", "KeyStore", "default_env_file"]
