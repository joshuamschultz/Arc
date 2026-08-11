"""``arc keys`` — set, list, and remove the API keys Arc's LLM providers read.

SPEC-064 T-010. Before this verb, ``arc init`` ended by printing
``echo 'ANTHROPIC_API_KEY=sk-...' >> ~/.arc/.env`` and leaving the operator to
hand-edit a dotfile — with no permission guarantee, no audit record, and no way
for any other surface to do the same thing.

Two constraints shape the surface:

* **A value is only ever typed, never passed.** ``set`` collects the key with
  ``getpass``, which does not echo, and there is deliberately no ``--value`` flag:
  a credential on argv lands in shell history and in the process table. This is
  the line ``connector.py`` states in its own docstring, and it is why key setup
  is an operator action rather than something an agent could be asked to do.
* **A value is never read back.** ``list`` answers ``set`` / ``not set`` and
  nothing else (D-583) — no prefix, no length. The value is read from the process
  environment by arcllm when a call is made; nothing here reads it.

Which environment variable a provider needs comes from ``arcllm``, the one
declarer (D-581), so a provider that ships in arcllm is settable here the same day.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import sys
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any, NoReturn, TypeVar

import arcagent
import arcllm

from arccli.commands._shared import dispatch, err
from arccli.commands._shared import print_json as _print_json
from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _out

T = TypeVar("T")


def _arc_dir(args: argparse.Namespace) -> Path:
    """The Arc config home this invocation acts on."""
    given = getattr(args, "arc_dir", None)
    return Path(given).expanduser() if given else Path.home() / ".arc"


def _data_dir(args: argparse.Namespace) -> Path:
    """The operational data directory — arcstore's, unless the operator names one."""
    given = getattr(args, "data_dir", None)
    if given:
        path = Path(given).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path
    from arcstore import resolve_data_dir

    return Path(resolve_data_dir(None))


def _audit_sink(arc_dir: Path, data_dir: Path) -> Any:
    """The operator-signed WORM sink every key mutation lands in.

    The caller MUST close it: the sink holds an exclusive ``flock`` for its
    lifetime, so an unclosed one locks every later writer out of the chain.
    """
    from arcstore.ingest import WORM_ACTIVE_FILENAME
    from arctrust import WormSink

    from arccli.commands.operator import resolve_operator_signer, resolve_record_cipher

    worm_dir = data_dir / "worm"
    worm_dir.mkdir(parents=True, exist_ok=True)
    return WormSink(
        worm_dir / WORM_ACTIVE_FILENAME,
        resolve_operator_signer(arc_dir),
        cipher=resolve_record_cipher(arc_dir),
    )


@contextlib.contextmanager
def _audited(args: argparse.Namespace) -> Iterator[tuple[arcagent.KeyStore, str]]:
    """Open the store and the audit chain for one command, and always close the chain."""
    arc_dir = _arc_dir(args)
    sink = _audit_sink(arc_dir, _data_dir(args))
    try:
        yield arcagent.KeyStore(arcagent.default_env_file(arc_dir), sink=sink), _actor_did(arc_dir)
    finally:
        sink.close()


def _actor_did(arc_dir: Path) -> str:
    """The operator's own DID, stamped on every key mutation.

    Resolved from the same on-disk operator key every other CLI operator surface
    uses (``arc approve``, ``arc workflow``) — never an agent identity, because
    setting a provider key is an operator act.
    """
    from arctrust.identity import did_from_public_key

    from arccli.commands.operator import load_operator_key, operator_public_key

    public = operator_public_key(arc_dir) or load_operator_key(arc_dir).public_key
    return did_from_public_key(public, org="local", agent_type="operator")


def _env_var_for(provider: str) -> str:
    """Which variable this provider reads, per arcllm. Exits naming the provider if none."""
    declared = {key.provider: key.api_key_env for key in arcllm.list_provider_keys()}
    if provider not in declared:
        _fail(f"unknown provider {provider!r} — arcllm packages: {', '.join(declared)}")
    return declared[provider]


def _fail(message: str) -> NoReturn:
    """Report the problem to stderr and stop. Never returns."""
    err(f"arc keys: {message}")
    sys.exit(1)


def _run(call: Coroutine[Any, Any, T]) -> T:
    """Run one store call, turning its refusal into an operator-facing exit.

    The store names the coordinate and never the rejected value, so its message is
    safe to print verbatim.
    """
    try:
        return asyncio.run(call)
    except arcagent.ArcAgentError as exc:
        _fail(exc.message)


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _list(args: argparse.Namespace) -> None:
    """Show every provider arcllm packages and whether its key is stored."""
    with _audited(args) as (store, did):
        statuses = _run(store.list(caller_did=did))
    if args.json:
        _print_json([_row(status) for status in statuses])
        return
    _print_table(
        ["Provider", "Env var", "Required", "Status"],
        [
            [
                status.provider,
                status.env_var,
                "yes" if status.required else "no",
                "set" if status.present else "not set",
            ]
            for status in statuses
        ],
    )


def _row(status: arcagent.KeyStatus) -> dict[str, Any]:
    """The scriptable shape — presence only, never a value (D-583)."""
    return {
        "provider": status.provider,
        "env_var": status.env_var,
        "required": status.required,
        "present": status.present,
    }


def _set(args: argparse.Namespace) -> None:
    """Store one provider's key, collected with a prompt that does not echo."""
    env_var = _env_var_for(args.provider)
    value = getpass.getpass(f"{env_var} (hidden): ")
    with _audited(args) as (store, did):
        _run(store.set(env_var, value, caller_did=did))
    _out(f"Stored {env_var} for {args.provider} in {arcagent.default_env_file(_arc_dir(args))}.")


def _remove(args: argparse.Namespace) -> None:
    """Forget one provider's key. Never an error when there was nothing to remove."""
    env_var = _env_var_for(args.provider)
    with _audited(args) as (store, did):
        removed = _run(store.delete(env_var, caller_did=did))
    _out(
        f"Removed {env_var} for {args.provider}."
        if removed
        else f"No key was stored for {args.provider} ({env_var}); nothing to remove."
    )


# ---------------------------------------------------------------------------
# Argparse-based dispatcher
# ---------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    """Where this invocation's world lives, so a fleet operator can target one."""
    parser.add_argument("--arc-dir", default=None, help="Arc config dir (default: ~/.arc).")
    parser.add_argument("--data-dir", default=None, help="Operational data dir for audit.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc keys",
        description="Provider API keys — list, set (hidden prompt), remove.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    p = subs.add_parser("list", help="Show every provider and whether its key is set.")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    _add_common(p)

    # No --value: a credential on argv is shell history and a process-table entry.
    p = subs.add_parser("set", help="Store a provider's API key (hidden prompt).")
    p.add_argument("provider", help="Provider name, as arcllm packages it.")
    _add_common(p)

    p = subs.add_parser("remove", help="Forget a provider's stored API key.")
    p.add_argument("provider", help="Provider name, as arcllm packages it.")
    _add_common(p)

    return parser


_SUBCOMMAND_MAP = {"list": _list, "set": _set, "remove": _remove}


def keys_handler(args: list[str]) -> None:
    """Top-level handler for ``arc keys <sub> [args]``."""
    dispatch(_build_parser(), _SUBCOMMAND_MAP, args)


__all__ = ["keys_handler"]
