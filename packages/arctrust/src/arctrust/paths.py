"""Arc-home path resolution — the single source of truth for ``~/.arc``.

The Arc home is split by **lifecycle**, because updating Arc is meant to be
"download the new one and overwrite it" and three of the four things living
there must survive that:

===================  ==============================================  ==================
Root                 Holds                                           On update
===================  ==============================================  ==================
``runtime/``         the framework — code, venv, modules             replaced wholesale
``config/``          ``arcagent.toml`` … ``gateway.toml``, ``arc.env``  preserved
``state/``           operator key, identity, trust store, arcstore,   never touched
                     NATS JetStream, bundles
``team/``            per-agent traces, sessions, memory, workspace    never touched
===================  ==============================================  ==================

Runtimes install side by side under ``runtime/<version>/`` — the whole framework,
including its own virtualenv (:func:`runtime_venv`) — with a ``current`` symlink,
so an update is an atomic symlink flip (:func:`activate_runtime`) and a rollback
is flipping it back.

That split only holds if the install is genuinely the disposable thing. A
deployment whose code and venv sit in a git checkout has no disposable install —
the directory an update replaces and the directory an operator pulls into are the
same one — and a fleet inside that checkout is durable data in a disposable tree,
which is how a production ``git pull`` came to collide with running agents'
memory. Everything a service unit executes resolves through
:func:`runtime_bin`; everything durable is a sibling root the runtime cannot
reach.

**One resolver per concern.** Every path below is a named function. Callers must
never compose their own — ``arc_home() / "operator"`` reads the *pre-split*
location and would silently lose the operator signing key after a migration;
lose that key and every WORM audit chain it signed becomes unverifiable. The
rule is enforced by ``tests/architecture/test_arc_home_single_resolver.py``.

**Resolved per call, never cached at import.** ``ARC_CONFIG_DIR`` is routinely
exported *after* this module is first imported — by a service unit, by a test,
by ``arc up``. A module-level constant would freeze whatever the environment
said at import time, which is exactly how the earlier resolver split happened.
"""

from __future__ import annotations

import os
from pathlib import Path

ARC_CONFIG_DIR_ENV = "ARC_CONFIG_DIR"
"""Environment override relocating the entire Arc home."""

#: Relocates the FLEET root only. Separate from ARC_CONFIG_DIR because agent data
#: and Arc's own home have different lifecycles: an update may replace parts of
#: the home and must never be able to reach a running agent's memory.
ARC_TEAM_ROOT_ENV = "ARC_TEAM_ROOT"

_CURRENT = "current"
"""Name of the symlink pointing at the active runtime version."""

PRE_SYMLINK_PREFIX = f"{_CURRENT}.pre-symlink."
"""Prefix :func:`activate_runtime` gives the real ``current/`` directory it rescues.

A deployment made before the symlink existed has a real directory there. It is
kept, never deleted — its modules may be the only record of what the box had —
but it is a rescued artifact, not an installed runtime, so anything listing
versions must exclude it rather than offer a venv-less tree for activation.
"""

OPERATOR_KEY_FILENAME = "operator.key"
"""The operator key's filename, for the one caller that supplies its own directory.

``security.operator_key_dir`` lets an operator name a custody directory; that
caller still must not re-spell the filename, because two spellings of the key
that signs every WORM chain is the same defect as two spellings of its path.
"""

#: Type of the optional explicit root every accessor accepts.
Base = Path | str | None


def _base(base: Base = None) -> Path:
    """Resolve the Arc home an accessor should answer against.

    ``None`` — the ordinary case — means this process's Arc home. A value is a
    caller-supplied root, which is what ``--arc-dir`` / ``--dir`` mean: operate
    on *that* deployment's tree. Passing it here rather than joining by hand is
    what keeps the layout knowledge in one place for both.
    """
    return Path(base).expanduser() if base else arc_home()


# ---------------------------------------------------------------------------
# The four roots
# ---------------------------------------------------------------------------


def arc_home() -> Path:
    """Return the Arc home: ``${ARC_CONFIG_DIR:-~/.arc}``.

    The parent of all four lifecycle roots. Prefer :func:`arc_config`,
    :func:`arc_state`, :func:`arc_runtime`, or :func:`arc_team` — this exists
    for the migration and for surfaces that genuinely mean "the whole tree".
    """
    base = os.environ.get(ARC_CONFIG_DIR_ENV)
    return Path(base).expanduser() if base else Path.home() / ".arc"


def arc_config(base: Base = None) -> Path:
    """Return the preserved config root: ``<arc_home>/config``.

    Operator-authored TOML and the provider-key env file. An update leaves it
    exactly as it found it.
    """
    return _base(base) / "config"


def arc_state(base: Base = None) -> Path:
    """Return the never-touched state root: ``<arc_home>/state``.

    Everything irreplaceable: the operator signing key, agent identities, the
    trust store, the arcstore database, NATS JetStream state, staged bundles.
    An update must not read, move, or rewrite anything under here.
    """
    return _base(base) / "state"


def arc_runtime_root(base: Base = None) -> Path:
    """Return the runtime root holding every installed version: ``<arc_home>/runtime``."""
    return _base(base) / "runtime"


def arc_runtime(base: Base = None) -> Path:
    """Return the active runtime: ``<arc_home>/runtime/current``.

    A symlink into a ``runtime/<version>/`` sibling. Everything under it is
    disposable — replaced wholesale on update, rebuilt by ``arc install``.
    """
    return arc_runtime_root(base) / _CURRENT


def arc_runtime_version(version: str, base: Base = None) -> Path:
    """Return the install directory for one runtime version: ``runtime/<version>``."""
    return arc_runtime_root(base) / _validated_version(version)


def arc_team(name: str = "team", base: Base = None) -> Path:
    """Return a fleet root: ``${ARC_TEAM_ROOT:-<arc_home>}/<name>`` (default ``team``).

    Per-agent traces, sessions, memory, and workspace — the operator's own work,
    and the one root here that cannot be regenerated from anything.

    It is a **sibling** of ``runtime/``, never a child, and it is outside any code
    checkout. Both matter and both have cost a live box: a fleet under the runtime
    is destroyed by an update, and a fleet under the checkout (the old ``~/arc``
    default) put 1,700 files of agent memory where every ``git pull`` collides
    with them. ``~/.arc`` is neither — nothing pulls into it, and an update only
    ever replaces ``runtime/<version>/``.

    ``ARC_TEAM_ROOT`` relocates the fleet alone, for an operator who wants agent
    data on its own disk. ``base`` (i.e. ``--arc-dir``) still wins when given, so
    a self-contained deployment can put everything under one root.

    Precedence, most specific first: explicit ``base`` → ``ARC_TEAM_ROOT`` →
    :func:`arc_home`. Following the home is what keeps an isolated run isolated:
    resolving a fixed path while the rest of the home was redirected is how a
    test run creates agents in the developer's own live fleet.
    """
    if base is not None:
        return _base(base) / name
    override = os.environ.get(ARC_TEAM_ROOT_ENV)
    root = Path(override).expanduser() if override else arc_home()
    return root / name


# ---------------------------------------------------------------------------
# config/ — preserved across an update
# ---------------------------------------------------------------------------


def config_file(name: str, base: Base = None) -> Path:
    """Return one deployment config file: ``<arc_config>/<name>``.

    ``name`` is a bare filename — ``arcagent.toml``, ``arcllm.toml``,
    ``arcrun.toml``, ``gateway.toml``, ``connections.toml``.
    """
    return arc_config(base) / name


def env_file(base: Base = None) -> Path:
    """Return the provider-key env file: ``<arc_config>/arc.env``.

    One file for every surface that sets or reports a provider key. A key set in
    one surface and invisible to another is indistinguishable from a key that
    failed to save, which is the worst failure this store can have.
    """
    return arc_config(base) / "arc.env"


def dotenv_file(base: Base = None) -> Path:
    """Return the deployment's plain ``.env``: ``<arc_config>/.env``.

    Distinct from :func:`env_file`: that one is Arc's own managed provider-key
    store, this is the file an operator drops in by hand so a self-contained
    deployment starts up without exporting anything.
    """
    return arc_config(base) / ".env"


# ---------------------------------------------------------------------------
# state/ — never touched by an update
# ---------------------------------------------------------------------------


def operator_dir(base: Base = None) -> Path:
    """Return the operator-key custody dir: ``<arc_state>/operator``."""
    return arc_state(base) / "operator"


def default_operator_key_path(base: Base = None) -> Path:
    """Return the on-box operator key: ``<arc_state>/operator/operator.key``.

    The deployment's audit authority (see :mod:`arctrust.operator`), living
    outside any agent workspace tool-sandbox. It signs every WORM chain record;
    destroy it and the chains it signed can no longer be verified.
    """
    return operator_dir(base) / OPERATOR_KEY_FILENAME


def witness_dir(base: Base = None) -> Path:
    """Return the federal witness medium's dir: ``<arc_state>/witness``.

    Deliberately NOT under :func:`operator_dir`: the witness exists to make a
    rollback detectable *by someone holding the operator key*, so an operator who
    also owns the witness makes the check illusory (SPEC-053 REQ-019).
    """
    return arc_state(base) / "witness"


def default_witness_medium_path(base: Base = None) -> Path:
    """Return the witness anchor log: ``<arc_state>/witness/anchor.log``."""
    return witness_dir(base) / "anchor.log"


def identity_dir(base: Base = None) -> Path:
    """Return the signing-authority key dir: ``<arc_state>/identity``."""
    return arc_state(base) / "identity"


def trust_dir(base: Base = None) -> Path:
    """Return the trust store: ``<arc_state>/trust`` (operators.toml, issuers.toml)."""
    return arc_state(base) / "trust"


def store_dir(base: Base = None) -> Path:
    """Return the arcstore data dir: ``<arc_state>/store``."""
    return arc_state(base) / "store"


def nats_dir(base: Base = None) -> Path:
    """Return the NATS JetStream store: ``<arc_state>/nats``."""
    return arc_state(base) / "nats"


def extensions_dir(base: Base = None) -> Path:
    """Return the installed third-party extension root: ``<arc_state>/extensions``.

    Distinct from :func:`bundles_dir` on purpose. That directory stages the
    signed ``.arcbundle`` files a MODULE install materializes from; this one
    holds installed EXTENSIONS, which are third-party and adjudicated under the
    ``extension:*`` untrusted trust class. Pointing both at one directory made a
    module's staging store double as the extension search path, so an operator's
    extensions went looking in the wrong place after the home split.
    """
    return arc_state(base) / "extensions"


def bundles_dir(base: Base = None) -> Path:
    """Return the staged signed-bundle dir: ``<arc_state>/bundles``.

    Bundles are the signed inputs an install materializes *from*, so they
    outlive the runtime they were materialized into.
    """
    return arc_state(base) / "bundles"


def capabilities_dir(base: Base = None) -> Path:
    """Return the operator-curated global capabilities root: ``<arc_state>/capabilities``."""
    return arc_state(base) / "capabilities"


def blueprints_dir(base: Base = None) -> Path:
    """Return the user/shared blueprint dir: ``<arc_state>/blueprints``."""
    return arc_state(base) / "blueprints"


def skills_dir(base: Base = None) -> Path:
    """Return the hub-installed skill root: ``<arc_state>/skills``."""
    return arc_state(base) / "skills"


def gateway_dir(base: Base = None) -> Path:
    """Return the gateway's on-disk state root: ``<arc_state>/gateway``."""
    return arc_state(base) / "gateway"


def gateway_runtime_dir(base: Base = None) -> Path:
    """Return the gateway runtime dir: ``<arc_state>/gateway/run``."""
    return gateway_dir(base) / "run"


def gateway_pairing_db(base: Base = None) -> Path:
    """Return the gateway pairing database: ``<arc_state>/gateway/pairing.db``."""
    return gateway_dir(base) / "pairing.db"


def workflows_dir(base: Base = None) -> Path:
    """Return the signed workflow-bundle root: ``<arc_state>/workflows``.

    One root per deployment, shared by ``arc workflow``, the fleet runner, and
    any agent that authors a workflow. Resolving it anywhere else is what once
    made an agent-authored workflow real on disk and invisible to everything
    that could sign or run it.
    """
    return arc_state(base) / "workflows"


def audit_dir(base: Base = None) -> Path:
    """Return the deployment audit dir: ``<arc_state>/audit``."""
    return arc_state(base) / "audit"


def ui_token_file(base: Base = None) -> Path:
    """Return the loopback viewer token: ``<arc_state>/ui/viewer-token``.

    ``arc ui start`` writes it ``0600``; a same-user ``arc tui`` reads it to
    attach. Both sides resolve it here — it used to be "a filesystem contract,
    not shared code", which is a resolver split with extra steps.
    """
    return arc_state(base) / "ui" / "viewer-token"


def users_file(base: Base = None) -> Path:
    """Return the deployment user store: ``<arc_state>/users.json``."""
    return arc_state(base) / "users.json"


# ---------------------------------------------------------------------------
# runtime/ — replaced wholesale on update
# ---------------------------------------------------------------------------


def module_root(base: Base = None) -> Path:
    """Return the module root: ``<arc_runtime>/modules``.

    Modules do not ship inside the wheel; ``arc install`` materializes them from
    signed bundles in :func:`bundles_dir`. They ride with the code, so a new
    runtime starts empty and is refilled by an install — never hand-migrated.
    """
    return arc_runtime(base) / "modules"


def runtime_venv(base: Base = None) -> Path:
    """Return the active runtime's virtualenv: ``<arc_runtime>/.venv``.

    The venv belongs to the runtime version that owns it, so installing a new
    version installs a new environment beside the old one and the flip swaps
    both together. A venv shared across versions — or living in a checkout —
    turns "replace the runtime" back into an in-place upgrade with no rollback.
    """
    return arc_runtime(base) / ".venv"


def runtime_bin(name: str, base: Base = None) -> Path:
    """Return an executable in the active runtime: ``<runtime_venv>/bin/<name>``.

    ``arc``, ``python``, ``arc-agent-worker``. This is what a service unit and a
    deploy script must name — resolving it here is what stops one of them
    executing a checkout's copy while the other updates the runtime's.
    """
    return runtime_venv(base) / "bin" / name


def _validated_version(version: str) -> str:
    """Reject anything that is not a single directory name.

    A version string names a sibling under ``runtime/``. Letting it contain a
    separator or ``..`` would let a crafted value point ``current`` at an
    arbitrary directory — including ``state/``.
    """
    if not version or version in {".", ".."} or "/" in version or os.sep in version:
        raise ValueError(f"runtime version must be a single directory name, got {version!r}")
    return version


def activate_runtime(version: str, base: Base = None) -> Path:
    """Point ``runtime/current`` at ``runtime/<version>``. Atomic and reversible.

    The symlink is created under a temporary name and then ``os.replace``d onto
    ``current``, so a reader either sees the old target or the new one — never a
    missing link. Rolling back is calling this again with the previous version.

    Args:
        version: An installed runtime version (a bare directory name).

    Returns:
        The ``current`` symlink path.

    Raises:
        ValueError: ``version`` is not a single directory name.
        FileNotFoundError: that version is not installed.
    """
    target = arc_runtime_version(version, base)
    if not target.is_dir():
        raise FileNotFoundError(f"runtime version not installed: {target}")

    link = arc_runtime(base)
    unique = f"{os.getpid()}.{os.urandom(4).hex()}"
    staging = link.with_name(f".{_CURRENT}.staging.{unique}")
    staging.symlink_to(target, target_is_directory=True)

    # os.replace refuses to put a symlink over a real directory, and a pre-symlink
    # install can have exactly that. Move it aside — under a name that cannot
    # already exist, so a second activation never clobbers the first one's copy —
    # so the flip still lands.
    if link.is_dir() and not link.is_symlink():
        link.rename(link.with_name(f"{PRE_SYMLINK_PREFIX}{unique}"))
    os.replace(staging, link)
    return link


__all__ = [
    "ARC_CONFIG_DIR_ENV",
    "ARC_TEAM_ROOT_ENV",
    "OPERATOR_KEY_FILENAME",
    "PRE_SYMLINK_PREFIX",
    "Base",
    "activate_runtime",
    "arc_config",
    "arc_home",
    "arc_runtime",
    "arc_runtime_root",
    "arc_runtime_version",
    "arc_state",
    "arc_team",
    "audit_dir",
    "blueprints_dir",
    "bundles_dir",
    "capabilities_dir",
    "config_file",
    "default_operator_key_path",
    "default_witness_medium_path",
    "dotenv_file",
    "env_file",
    "gateway_dir",
    "gateway_pairing_db",
    "gateway_runtime_dir",
    "identity_dir",
    "module_root",
    "nats_dir",
    "operator_dir",
    "runtime_bin",
    "runtime_venv",
    "skills_dir",
    "store_dir",
    "trust_dir",
    "ui_token_file",
    "users_file",
    "witness_dir",
    "workflows_dir",
]
