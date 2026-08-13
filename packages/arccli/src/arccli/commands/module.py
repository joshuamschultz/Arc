"""``arc module`` — the operator surface for signed module bundles (SPEC-066).

A module reaches a deployment as a signed bundle and nowhere else. This group is
the whole lifecycle an operator drives::

    module list                        what is bundled, installed, and enabled
    module bundle <names...>           package modules from the source catalog
    module install <names...>          verify, materialize, copy, enable
    module install --all               everything the deployment permits
    module install --from <bundle>     the air-gapped path (no network, ever)
    module install --from-source <n>   the development inner loop (dev-signed)
    module remove <name>               the exact inverse of install

Three properties hold across all of it:

**Nothing is written before everything is verified.** The command verifies every
bundle in the request first and materializes only afterwards, so one bad bundle
in a batch leaves the deployment byte-identical to its prior state (REQ-326/327).
The guarantee itself lives in ``arcbundle``; this layer only refrains from
routing around it.

**Install and enable are one step.** A materialized module that no config enables
is a capability sitting on disk doing nothing — the state the whole spec exists
to eliminate. Install writes the ``[modules.NAME]`` entry, and remove drops it.

**The tier is read, never passed.** No flag selects the stringency a bundle is
verified at: it is the stricter of the deployment's ``[security].tier`` and the
target agent's, so a federal agent cannot be handed a dev-signed module by
running the install from a machine whose config says personal.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import arcagent
import arcbundle
import tomlkit
from arctrust import AuditSink, arc_home, generate_keypair

from arccli.commands._shared import dispatch
from arccli.commands._shared import err as _err
from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _out

#: Where staged bundles live. ``module bundle`` writes here and ``module
#: install <name>`` reads here, so the offline round trip needs no flags at all.
BUNDLE_DIR = "bundles"
BUNDLE_SUFFIX = ".arcbundle"

#: Env override for the module source catalog. Once modules leave the wheel
#: (T-970) the catalog is a repository checkout rather than an installed
#: package, and a build host needs to say where it is.
_SOURCE_ENV = "ARC_MODULE_SOURCE"

#: Increasing stringency. Used to take the stricter of two configured tiers —
#: never to widen one.
_TIER_ORDER = ("personal", "enterprise", "federal")

_UNRESOLVED_OPERATOR_DID = "did:arc:operator:unresolved"


# ---------------------------------------------------------------------------
# Deployment locations
# ---------------------------------------------------------------------------


def _bundle_store() -> Path:
    """The staged-bundle directory: ``${ARC_CONFIG_DIR:-~/.arc}/bundles``."""
    return arc_home() / BUNDLE_DIR


def _module_root() -> Path:
    """The deployment module root the agent actually scans.

    Resolved through ``arcagent``'s own facade rather than composed here: two
    places spelling out the same path is how a moved root leaves a reader behind
    (the ``[llm]`` config split did exactly that).
    """
    return arcagent.module_root()


def _source_catalog() -> Path:
    """The module source catalog ``module bundle`` packages from."""
    override = os.environ.get(_SOURCE_ENV)
    catalog = Path(override).expanduser() if override else arcagent.modules_path()
    if not catalog.is_dir():
        _err(
            f"arc module: no module source catalog at {catalog}. "
            f"Point {_SOURCE_ENV} at a checkout's arcagent/modules directory."
        )
        sys.exit(1)
    return catalog


# ---------------------------------------------------------------------------
# Tier, agents, and issuers
# ---------------------------------------------------------------------------


def _configured_tier(config_path: Path) -> str | None:
    """Read ``[security].tier`` from a TOML file, or None when it says nothing.

    A file that exists but cannot be parsed stops the command: guessing
    ``personal`` for an unreadable federal config would verify a dev-signed
    bundle on a box that forbids one.
    """
    if not config_path.is_file():
        return None
    try:
        block = tomllib.loads(config_path.read_text(encoding="utf-8")).get("security", {})
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        _err(f"arc module: cannot read the security tier from {config_path}: {exc}")
        sys.exit(1)
    tier = block.get("tier") if isinstance(block, dict) else None
    if tier is None:
        return None
    if tier not in _TIER_ORDER:
        _err(f"arc module: {config_path} declares an unknown tier {tier!r}")
        sys.exit(1)
    return str(tier)


def _verification_tier(agent_root: Path | None) -> str:
    """The stricter of the machine tier and the target agent's tier.

    Bundle stringency is a property of the box, but an agent may be hardened
    beyond it. Taking the maximum means installing for a federal agent applies
    federal rules even when the machine config has not caught up.
    """
    candidates = [_configured_tier(arc_home() / "arcagent.toml")]
    if agent_root is not None:
        candidates.append(_configured_tier(agent_root / "arcagent.toml"))
    found = [tier for tier in candidates if tier is not None]
    if not found:
        return "personal"
    return max(found, key=_TIER_ORDER.index)


@dataclass(frozen=True)
class _Agent:
    """The agent an install or removal targets."""

    agent_id: str
    root: Path

    @property
    def config_path(self) -> Path:
        return self.root / "arcagent.toml"


def _team_root() -> Path:
    """The deployment team dir — ``./team`` when present, else the cwd itself."""
    cwd_team = Path.cwd() / "team"
    return cwd_team if cwd_team.is_dir() else Path.cwd()


def _agents() -> list[_Agent]:
    """Every agent under the deployment team dir, in roster order."""
    from arcgateway import team_roster

    return [
        _Agent(agent_id=entry.agent_id, root=Path(entry.workspace_path))
        for entry in team_roster.list_team(team_root=_team_root(), online_ids=set())
    ]


def _resolve_agent(agent_arg: str | None) -> _Agent:
    """Resolve ``--agent`` to the agent this command acts on, or exit.

    A module is enabled per agent, so there is no defensible "all agents"
    default: silently enabling a capability on an agent the operator did not
    name is exactly the excessive agency (LLM06) this spec is closing.
    """
    entries = _agents()
    if not entries:
        _err(f"arc module: no agents found under {_team_root()}")
        sys.exit(1)
    if agent_arg:
        match = next((entry for entry in entries if entry.agent_id == agent_arg), None)
        if match is None:
            known = ", ".join(entry.agent_id for entry in entries)
            _err(f"arc module: unknown agent {agent_arg!r}. Known agents: {known}")
            sys.exit(1)
        return match
    if len(entries) > 1:
        known = ", ".join(entry.agent_id for entry in entries)
        _err(f"arc module: multiple agents ({known}); specify --agent <id>")
        sys.exit(1)
    return entries[0]


def _resolve_agent_optional(agent_arg: str | None) -> _Agent | None:
    """Like :func:`_resolve_agent`, but None instead of an exit when ambiguous.

    ``module list`` reports deployment state that is true with no agent at all,
    so it must still work on a box that has none.
    """
    try:
        entries = _agents()
    except Exception:  # reason: listing must survive an absent/broken roster
        return None
    if agent_arg:
        return next((entry for entry in entries if entry.agent_id == agent_arg), None)
    return entries[0] if len(entries) == 1 else None


def _operator_identity() -> tuple[str, bytes] | None:
    """The on-box operator DID and public key, or None when no key exists.

    Read-only: it never mints a key. The operator key is already the trust
    anchor a blueprint and a prompt overlay are pinned to, so a bundle this
    deployment built and signed itself verifies with no extra setup — while a
    bundle signed by anyone else still needs an explicit issuer entry.

    ``arc_home()`` is passed explicitly because the module commands are
    ARC_CONFIG_DIR-scoped end to end: an isolated deployment must not reach into
    the invoking user's real ``~/.arc`` for the key that decides what installs.
    """
    from arctrust.policy import OperatorApprovalAuthority

    from arccli.commands.operator import operator_public_key, resolve_operator_signer

    try:
        public_key = operator_public_key(arc_home())
        if public_key is None:
            return None
        return OperatorApprovalAuthority(resolve_operator_signer(arc_home())).did, public_key
    except Exception:  # reason: an unresolvable operator key means "not trusted", not a crash
        return None


def _peek_issuer(bundle_root: Path) -> str:
    """Read the issuer a bundle *claims*, only in order to look up a key for it.

    Reading an unverified field is safe here and unavoidable everywhere: a name
    is not a permission. An unknown name resolves to no key and the bundle is
    refused; a known name still has to produce that issuer's signature. This is
    the same order ``arcrun``'s manifest verifier already uses.
    """
    try:
        data = json.loads((bundle_root / arcbundle.MANIFEST_NAME).read_bytes())
    except (OSError, ValueError):
        return ""
    issuer = data.get("issuer") if isinstance(data, dict) else None
    return issuer if isinstance(issuer, str) else ""


def _trusted_issuers(bundle_root: Path) -> dict[str, bytes]:
    """The issuer keys this deployment accepts for ``bundle_root``.

    Two sources, both explicit: the on-box operator key, and the claimed issuer's
    entry in ``~/.arc/trust/issuers.toml``. A trust-store failure leaves the
    issuer out of the mapping rather than raising — absence is refusal, which is
    the fail-closed direction — but it is reported so an operator learns that a
    permissions or syntax problem, not a policy decision, is what turned the
    bundle away.
    """
    from arctrust.trust_store import TrustStoreError, load_issuer_pubkey

    trusted: dict[str, bytes] = {}
    operator = _operator_identity()
    if operator is not None:
        trusted[operator[0]] = operator[1]

    claimed = _peek_issuer(bundle_root)
    if claimed and claimed not in trusted:
        try:
            trusted[claimed] = load_issuer_pubkey(claimed)
        except TrustStoreError as exc:
            if exc.code != "TRUST_STORE_DID_UNKNOWN":
                _err(f"arc module: trust store unusable for issuer {claimed!r} — {exc}")
    return trusted


@contextlib.contextmanager
def _audit_chain() -> Iterator[tuple[AuditSink, str]]:
    """Open the deployment's operator-signed WORM chain for one module change.

    Held open for the whole command so an install of several bundles lands as
    one contiguous run of records. An unopenable chain degrades to a discarding
    sink: auditing never interrupts the action it audits (NIST AU-5).
    """
    from arcstore import resolve_data_dir
    from arctrust import NullSink

    from arccli.commands.operator import operator_worm_sink

    operator = _operator_identity()
    actor = operator[0] if operator is not None else _UNRESOLVED_OPERATOR_DID
    try:
        sink = operator_worm_sink(arc_home(), resolve_data_dir(None))
    except (OSError, RuntimeError, ValueError) as exc:
        _err(f"arc module: audit chain unavailable ({type(exc).__name__}); change not recorded")
        yield NullSink(), actor
        return
    try:
        yield sink, actor
    finally:
        sink.close()


# ---------------------------------------------------------------------------
# Config entry — install and enable are one step
# ---------------------------------------------------------------------------


def _write_atomic(path: Path, document: Any) -> None:
    """Replace ``path`` with ``document`` in one rename, never a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        with handle:
            tomlkit.dump(document, handle)
        os.replace(handle.name, path)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def _load_document(path: Path) -> Any:
    """Parse an agent config for editing, preserving comments and key order."""
    if not path.is_file():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except Exception as exc:  # reason: a malformed config must not be overwritten
        _err(f"arc module: cannot parse {path}: {exc}")
        sys.exit(1)


def enable_module(config_path: Path, module: str) -> None:
    """Write ``[modules.<module>] enabled = true`` into an agent's config."""
    document = _load_document(config_path)
    modules = document.get("modules")
    if modules is None:
        modules = tomlkit.table(is_super_table=True)
        document["modules"] = modules
    entry = modules.get(module)
    if entry is None:
        entry = tomlkit.table()
        modules[module] = entry
    entry["enabled"] = True
    _write_atomic(config_path, document)


def disable_module(config_path: Path, module: str) -> bool:
    """Drop ``[modules.<module>]`` entirely. True when an entry was removed.

    Setting ``enabled = false`` would leave the module named in a config it is
    no longer installed for — a listing that reports a capability the box does
    not have is the ambiguity this spec removes.
    """
    document = _load_document(config_path)
    modules = document.get("modules")
    if modules is None or module not in modules:
        return False
    del modules[module]
    if not modules:
        del document["modules"]
    _write_atomic(config_path, document)
    return True


def _enabled_modules(config_path: Path) -> set[str]:
    """The module names an agent's config currently enables."""
    if not config_path.is_file():
        return set()
    try:
        config = arcagent.load_config(config_path)
    except Exception:  # reason: listing degrades rather than failing on a bad config
        return set()
    return {name for name, entry in config.modules.items() if entry.enabled}


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _staged_bundles() -> dict[str, Path]:
    """Bundle name to bundle path for everything in the staging directory."""
    store = _bundle_store()
    if not store.is_dir():
        return {}
    return {
        path.name[: -len(BUNDLE_SUFFIX)]: path
        for path in sorted(store.iterdir())
        if path.is_dir() and path.name.endswith(BUNDLE_SUFFIX)
    }


def _installed_modules() -> set[str]:
    """Module names materialized at the deployment root."""
    return set(arcagent.discover_modules())


def _catalog_modules() -> set[str]:
    """Module names present in the source catalog, when one is reachable."""
    override = os.environ.get(_SOURCE_ENV)
    catalog = Path(override).expanduser() if override else arcagent.modules_path()
    if not catalog.is_dir():
        return set()
    return {path.name for path in catalog.iterdir() if path.is_dir() and not _is_private(path)}


def _is_private(path: Path) -> bool:
    return path.name.startswith("_") or path.name.startswith(".")


def _list(args: argparse.Namespace) -> None:
    agent = _resolve_agent_optional(getattr(args, "agent", None))
    staged = _staged_bundles()
    installed = _installed_modules()
    enabled = _enabled_modules(agent.config_path) if agent is not None else set()

    known = sorted(set(staged) | installed | enabled | _catalog_modules())
    if not known:
        _out("No modules known: none bundled, none installed, none enabled.")
        return

    rows = [
        [
            name,
            "yes" if name in staged else "-",
            "yes" if name in installed else "-",
            ("yes" if name in enabled else "-") if agent is not None else "?",
        ]
        for name in known
    ]
    label = agent.agent_id if agent is not None else "no agent resolved"
    _print_table(["Module", "Bundled", "Installed", f"Enabled ({label})"], rows)
    _out(f"\nmodule root: {_module_root()}\nbundle store: {_bundle_store()}")


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def _requested_bundles(
    args: argparse.Namespace, scratch: contextlib.ExitStack
) -> tuple[list[Path], dict[str, bytes]]:
    """Resolve the argv into the bundles this install will verify.

    Returns the bundle directories plus any issuer keys that exist only for this
    invocation. That second value is empty for every path but ``--from-source``,
    which mints the key it signs with and therefore has to hand the verifier the
    matching public key — there is nowhere else it could come from.
    """
    source = getattr(args, "source", None)
    from_source: str | None = getattr(args, "from_source", None)
    names: Sequence[str] = getattr(args, "names", []) or []

    if from_source:
        if names or source or getattr(args, "all", False):
            _err(
                "arc module install: --from-source builds one module from the source "
                "catalog; do not combine it with names, --all, or --from"
            )
            sys.exit(1)
        return _build_dev_bundle(from_source, scratch)

    if source:
        if names:
            _err("arc module install: --from takes the bundle path; do not also name modules")
            sys.exit(1)
        path = Path(source).expanduser()
        if not path.is_dir():
            _err(f"arc module install: no bundle directory at {path}")
            sys.exit(1)
        return [path], {}

    staged = _staged_bundles()
    if getattr(args, "all", False):
        if names:
            _err("arc module install: --all installs everything; do not also name modules")
            sys.exit(1)
        if not staged:
            _err(f"arc module install: no bundles staged in {_bundle_store()}")
            sys.exit(1)
        return [staged[name] for name in sorted(staged)], {}

    if not names:
        _err(
            "arc module install: name at least one module, or pass --all, "
            "--from <bundle>, or --from-source <name>.\n"
            f"Staged bundles: {', '.join(sorted(staged)) or 'none'}"
        )
        sys.exit(1)

    missing = [name for name in names if name not in staged]
    if missing:
        _err(
            f"arc module install: no staged bundle for {', '.join(missing)} in "
            f"{_bundle_store()}. Build one with: arc module bundle {' '.join(missing)}"
        )
        sys.exit(1)
    return [staged[name] for name in names], {}


def _build_dev_bundle(
    name: str, scratch: contextlib.ExitStack
) -> tuple[list[Path], dict[str, bytes]]:
    """Package ``name`` from the source catalog, signed with a development key.

    The development inner loop, and deliberately the *long* way round: the module
    is packaged, signed, and then verified and materialized by the same functions
    a release bundle goes through. Symlinking the source catalog would be faster
    and was rejected as SDD D-641 — it creates a load path whose only guard is a
    tier check, so the path developers exercise every day would be the one path
    nobody's signature ever covered.

    Two properties keep the convenience from becoming a production door:

    * **The key is ephemeral.** It is minted here, used once, and never written
      anywhere. There is no dev key on disk to steal, reuse, or promote.
    * **The bundle is temporary.** It is built into a scratch directory that is
      deleted when the command returns, rather than into the deployment's bundle
      store, so a dev-signed bundle cannot linger where a later ``install --all``
      would find it.

    The tier rule is not enforced here. ``arcbundle.verifier`` trusts
    ``DEV_ISSUER`` at personal tier only and refuses it before it ever consults
    the issuer keys below, so this bundle dies at an enterprise or federal box no
    matter how the command was invoked.
    """
    catalog = _source_catalog()
    source = catalog / name
    if not source.is_dir():
        available = ", ".join(sorted(p.name for p in catalog.iterdir() if p.is_dir()))
        _err(
            f"arc module install --from-source: no module source for {name!r} in "
            f"{catalog}. Available: {available}"
        )
        sys.exit(1)

    out_dir = Path(scratch.enter_context(tempfile.TemporaryDirectory(prefix="arc-from-source-")))
    keypair = generate_keypair()
    try:
        bundle = arcbundle.build_bundle(
            source,
            module=name,
            version=arcagent.__version__,
            private_key=keypair.private_key,
            issuer=arcbundle.DEV_ISSUER,
            out=out_dir / f"{name}{BUNDLE_SUFFIX}",
        )
    except arcbundle.BundleError as exc:
        _err(f"arc module install --from-source: could not package {name} — {exc}")
        sys.exit(1)
    return [bundle], {arcbundle.DEV_ISSUER: keypair.public_key}


def _install(args: argparse.Namespace) -> None:
    agent = _resolve_agent(getattr(args, "agent", None))
    tier = _verification_tier(agent.root)
    permissive = bool(getattr(args, "all", False))
    modules_root = _module_root()

    with contextlib.ExitStack() as scratch:
        bundles, ephemeral_issuers = _requested_bundles(args, scratch)
        with _audit_chain() as (sink, actor):
            verified, skipped = _verify_all(
                bundles,
                tier=tier,
                sink=sink,
                actor=actor,
                permissive=permissive,
                ephemeral_issuers=ephemeral_issuers,
            )
            # Nothing above this line wrote a byte: a refusal in a batch leaves
            # the deployment exactly as it was (REQ-326/327).
            for bundle in verified:
                module = bundle.manifest.module
                installed = arcbundle.materialize(bundle, modules_root, sink=sink, actor_did=actor)
                arcbundle.copy_capabilities(installed, agent.root, module=module)
                enable_module(agent.config_path, module)
                _out(
                    f"Installed {module} {bundle.manifest.version} "
                    f"(issuer {bundle.manifest.issuer}, tier {tier}) — "
                    f"runtime {installed}, enabled for {agent.agent_id}."
                )

    for name, reason in skipped:
        _out(f"Skipped {name}: {reason}")
    if not verified:
        _out("Nothing installed.")


def _verify_all(
    bundles: Sequence[Path],
    *,
    tier: str,
    sink: AuditSink,
    actor: str,
    permissive: bool,
    ephemeral_issuers: Mapping[str, bytes],
) -> tuple[list[arcbundle.VerifiedBundle], list[tuple[str, str]]]:
    """Verify every requested bundle before any of them is written.

    ``permissive`` is ``--all``: a bundle this deployment is not *permitted* to
    install is skipped rather than failing the command (REQ-330). That leniency
    covers signature refusals only — an untrusted issuer or a tier that forbids
    the signer. A content-hash mismatch or a malformed manifest is tampering,
    not policy, and stops the command at every invocation.

    ``ephemeral_issuers`` carries the ``--from-source`` key, which exists only
    for this call. It is merged *under* the deployment's own trusted issuers so
    a per-invocation key can never displace a configured one, and it is empty on
    every other path — including ``--all``, which is why a dev key cannot be
    smuggled into a batch install.
    """
    verified: list[arcbundle.VerifiedBundle] = []
    skipped: list[tuple[str, str]] = []
    for bundle_root in bundles:
        try:
            verified.append(
                arcbundle.verify_bundle(
                    bundle_root,
                    tier=tier,
                    trusted_issuers={**ephemeral_issuers, **_trusted_issuers(bundle_root)},
                    sink=sink,
                    actor_did=actor,
                )
            )
        except arcbundle.BundleSignatureError as exc:
            if permissive:
                skipped.append((bundle_root.name, str(exc)))
                continue
            _err(f"arc module install: refused {bundle_root} — {exc}\nNothing was installed.")
            sys.exit(1)
        except arcbundle.BundleError as exc:
            _err(f"arc module install: refused {bundle_root} — {exc}\nNothing was installed.")
            sys.exit(1)
    return verified, skipped


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def _remove(args: argparse.Namespace) -> None:
    """Delete runtime, copies, and config entry — reporting every part that fails.

    All three are attempted even when one fails, because stopping at the first
    would leave a half-removed module whose remaining half is still loadable.
    Any failure exits non-zero with the explicit reason (REQ-338).
    """
    agent = _resolve_agent(getattr(args, "agent", None))
    module = args.name
    failures: list[str] = []
    done: list[str] = []

    with _audit_chain() as (sink, actor):
        try:
            arcbundle.remove(module, _module_root(), sink=sink, actor_did=actor)
            done.append("runtime")
        except arcbundle.BundleError as exc:
            failures.append(f"runtime: {exc}")

    try:
        if arcbundle.remove_capabilities(agent.root, module=module):
            done.append("capabilities")
    except arcbundle.BundleError as exc:
        failures.append(f"capabilities: {exc}")

    try:
        if disable_module(agent.config_path, module):
            done.append("config entry")
    except SystemExit:
        raise
    except OSError as exc:
        failures.append(f"config entry: {exc}")

    if failures:
        _err(f"arc module remove: {module} was not fully removed from {agent.agent_id}:")
        for failure in failures:
            _err(f"  {failure}")
        if done:
            _err(f"  (removed: {', '.join(done)})")
        sys.exit(1)

    removed = ", ".join(done) if done else "nothing — it was not installed"
    _out(f"Removed {module} from {agent.agent_id}: {removed}.")


# ---------------------------------------------------------------------------
# bundle
# ---------------------------------------------------------------------------


def _bundle(args: argparse.Namespace) -> None:
    """Package modules from the source catalog into signed bundles.

    Signed with the deployment operator key through ``arcbundle.build_bundle``,
    the same function release CI uses — the local path is the release path with
    a different key, never a shortcut around verification. Runs entirely offline,
    which is what makes it usable on a low-side staging host.
    """
    from arctrust.policy import OperatorApprovalAuthority

    from arccli.commands.operator import resolve_operator_signer

    catalog = _source_catalog()
    out_dir = Path(args.out).expanduser() if args.out else _bundle_store()
    signer = resolve_operator_signer(arc_home())
    issuer = OperatorApprovalAuthority(signer).did

    missing = [name for name in args.names if not (catalog / name).is_dir()]
    if missing:
        available = ", ".join(sorted(p.name for p in catalog.iterdir() if p.is_dir()))
        _err(
            f"arc module bundle: no module source for {', '.join(missing)}. Available: {available}"
        )
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    built: list[Path] = []
    for name in args.names:
        target = out_dir / f"{name}{BUNDLE_SUFFIX}"
        if target.exists():
            if not args.force:
                _err(f"arc module bundle: {target} already exists; pass --force to replace it")
                sys.exit(1)
            shutil.rmtree(target)
        try:
            built.append(
                arcbundle.build_bundle(
                    catalog / name,
                    module=name,
                    version=arcagent.__version__,
                    private_key=signer,
                    issuer=issuer,
                    out=target,
                )
            )
        except arcbundle.BundleError as exc:
            _err(f"arc module bundle: could not package {name} — {exc}")
            sys.exit(1)

    for path in built:
        _out(f"Built {path}")
    _out(f"\nSigned by {issuer}. Install with: arc module install {' '.join(args.names)}")


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc module",
        description="Signed module bundles — list, bundle, install, remove.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    p_list = subs.add_parser("list", help="Show bundled / installed / enabled state.")
    p_list.add_argument("--agent", dest="agent", default=None, help="Agent id under team/.")

    p_install = subs.add_parser("install", help="Verify, materialize, copy, and enable.")
    p_install.add_argument("names", nargs="*", help="Module names with staged bundles.")
    p_install.add_argument("--agent", dest="agent", default=None, help="Agent id under team/.")
    p_install.add_argument(
        "--all",
        dest="all",
        action="store_true",
        help="Install every staged bundle this deployment permits; skip the rest.",
    )
    p_install.add_argument(
        "--from",
        dest="source",
        default=None,
        metavar="<bundle>",
        help="Install from a pre-staged bundle directory (air-gapped).",
    )
    p_install.add_argument(
        "--from-source",
        dest="from_source",
        default=None,
        metavar="<name>",
        help="Development: bundle from the source catalog, dev-signed (personal tier only).",
    )

    p_remove = subs.add_parser("remove", help="Delete runtime, copies, and config entry.")
    p_remove.add_argument("name", help="Module name to remove.")
    p_remove.add_argument("--agent", dest="agent", default=None, help="Agent id under team/.")

    p_bundle = subs.add_parser("bundle", help="Package modules from the source catalog.")
    p_bundle.add_argument("names", nargs="+", help="Module names to package.")
    p_bundle.add_argument(
        "-o",
        "--out",
        dest="out",
        default=None,
        metavar="<dir>",
        help="Output directory (default: the deployment bundle store).",
    )
    p_bundle.add_argument(
        "--force", dest="force", action="store_true", help="Replace an existing bundle."
    )

    return parser


_SUBCOMMAND_MAP = {
    "list": _list,
    "install": _install,
    "remove": _remove,
    "bundle": _bundle,
}


def module_handler(args: list[str]) -> None:
    """Top-level handler for ``arc module <sub> [args]`` (registry dispatch)."""
    dispatch(_build_parser(), _SUBCOMMAND_MAP, args)
