"""`arc ext` — manage capability `.py` files (SPEC-021).

A capability file is a Python module that exposes one or more callables
or classes stamped by `@tool`, `@hook`, `@background_task`, or
`@capability`. The unified `CapabilityLoader` discovers them from four
scan roots:

  1. `arcagent/builtins/capabilities/`  (package, read-only)
  2. `~/.arc/capabilities/`              (global, this command's --global target)
  3. `<agent>/capabilities/`             (per-agent, trusted)
  4. `<agent>/workspace/capabilities/`  (agent-authored, untrusted, AST-validated)
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import shutil
import sys
from pathlib import Path

_logger = logging.getLogger("arccli.commands.ext")

_GLOBAL_CAP_DIR = Path.home() / ".arc" / "capabilities"

_TOOL_TEMPLATE = '''\
"""Capability: {name}

Registers a tool with the unified capability loader (SPEC-021).
"""

from __future__ import annotations

from arcagent.tools._decorator import tool


@tool(
    description="<one-sentence imperative description>",
    classification="state_modifying",   # change to "read_only" if no side effect
    capability_tags=["<tag>"],          # e.g., "file_read", "network_egress"
    when_to_use="When you need to ...",
    version="1.0.0",
)
async def {name}(arg: str) -> str:
    """Edit me — implement the tool body."""
    return f"hello from {name}: {{arg}}"
'''


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write(msg: str = "") -> None:
    sys.stdout.write(msg + "\n")


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    try:
        from arccli.formatting import print_table

        print_table(headers, rows)
    except ImportError:
        sys.stdout.write("  " + "  ".join(headers) + "\n")
        for row in rows:
            sys.stdout.write("  " + "  ".join(row) + "\n")


def _stamped_capabilities(module: object) -> list[tuple[str, str]]:
    """Return [(name, kind)] for every `_arc_capability_meta`-stamped value."""
    found: list[tuple[str, str]] = []
    for value in vars(module).values():
        meta = getattr(value, "_arc_capability_meta", None)
        if meta is None:
            continue
        kind = getattr(meta, "kind", "?")
        name = getattr(meta, "name", getattr(value, "__name__", "?"))
        found.append((str(name), str(kind)))
    return found


def _quick_has_decorator(py_file: Path) -> bool:
    """Cheap text check for any of the SPEC-021 decorator names."""
    try:
        content = py_file.read_text(encoding="utf-8")
    except Exception:  # reason: fail-open — continue
        return False
    return any(token in content for token in ("@tool", "@hook", "@background_task", "@capability"))


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _list(args: argparse.Namespace) -> None:
    """List capability files across the four scan roots."""
    agent_dir: str | None = getattr(args, "agent", None)

    dirs_to_scan: list[tuple[str, Path]] = []
    if _GLOBAL_CAP_DIR.is_dir():
        dirs_to_scan.append(("global", _GLOBAL_CAP_DIR))
    if agent_dir:
        agent_root = Path(agent_dir).expanduser().resolve()
        agent_caps = agent_root / "capabilities"
        ws_caps = agent_root / "workspace" / "capabilities"
        if agent_caps.is_dir():
            dirs_to_scan.append(("agent", agent_caps))
        if ws_caps.is_dir():
            dirs_to_scan.append(("workspace", ws_caps))

    rows: list[list[str]] = []
    for source, directory in dirs_to_scan:
        for py_file in sorted(directory.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            has_decorator = _quick_has_decorator(py_file)
            rows.append([py_file.stem, source, str(py_file), "yes" if has_decorator else "no"])

    if rows:
        _print_table(["Name", "Source", "Path", "Decorated"], rows)
        return

    _write("No capability files found.")
    _write(f"  Global dir: {_GLOBAL_CAP_DIR}")
    if agent_dir:
        agent_root = Path(agent_dir).expanduser().resolve()
        _write(f"  Agent dir:  {agent_root / 'capabilities'}")
        _write(f"  Workspace:  {agent_root / 'workspace' / 'capabilities'}")


def _create(args: argparse.Namespace) -> None:
    """Scaffold a new capability `.py` with a `@tool` decorator template."""
    name: str = args.name
    target_dir: str | None = getattr(args, "dir", None)
    use_global: bool = getattr(args, "use_global", False)

    if use_global:
        out_dir = _GLOBAL_CAP_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
    elif target_dir:
        out_dir = Path(target_dir).expanduser().resolve()
    else:
        out_dir = Path.cwd()

    out_file = out_dir / f"{name}.py"
    if out_file.exists():
        sys.stderr.write(f"Error: File already exists: {out_file}\n")
        sys.exit(1)

    out_file.write_text(_TOOL_TEMPLATE.format(name=name))
    _write(f"Created capability: {out_file}")
    _write()
    _write("Next steps:")
    _write(f"  1. Edit {out_file} to implement your tool")
    _write(f"  2. arc ext validate {out_file}")


def _install(args: argparse.Namespace) -> None:
    """Install a capability `.py` into ~/.arc/capabilities/."""
    source: str = args.source
    src = Path(source).expanduser().resolve()
    _GLOBAL_CAP_DIR.mkdir(parents=True, exist_ok=True)

    if src.is_file():
        dest = _GLOBAL_CAP_DIR / src.name
        if dest.exists():
            sys.stderr.write(f"Error: Already installed: {dest}\n")
            sys.exit(1)
        shutil.copy2(src, dest)
        _write(f"Installed: {src.name} -> {dest}")
        return

    if src.is_dir():
        copied = 0
        for py_file in sorted(src.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            dest = _GLOBAL_CAP_DIR / py_file.name
            if dest.exists():
                _write(f"  Skipped (exists): {py_file.name}")
                continue
            shutil.copy2(py_file, dest)
            _write(f"  Installed: {py_file.name}")
            copied += 1
        _write(f"\nInstalled {copied} capability file(s) to {_GLOBAL_CAP_DIR}")
        return

    sys.stderr.write(f"Error: Source not found: {src}\n")
    sys.exit(1)


def _validate(args: argparse.Namespace) -> None:
    """Import a capability file and confirm at least one decorated callable."""
    path: str = args.path
    cap_path = Path(path).expanduser().resolve()
    if not cap_path.exists():
        sys.stderr.write(f"Error: File not found: {cap_path}\n")
        sys.exit(1)
    if cap_path.suffix != ".py":
        sys.stderr.write(f"Error: Expected .py file: {cap_path}\n")
        sys.exit(1)

    module_name = f"arcagent_cap_validate_{cap_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, cap_path)
    if spec is None or spec.loader is None:
        sys.stderr.write(f"Error: Could not create import spec for: {cap_path}\n")
        sys.exit(1)

    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:  # reason: best-effort — record + continue
        _write(f"  [FAIL] Import error: {exc}")
        sys.exit(1)

    stamped = _stamped_capabilities(mod)
    if not stamped:
        _write("  [FAIL] No @tool / @hook / @background_task / @capability stamp found")
        sys.exit(1)

    _write(f"  [OK] {cap_path.name}")
    for name, kind in stamped:
        _write(f"       {kind}: {name}")
    _write(f"       Path:    {cap_path}")


# ---------------------------------------------------------------------------
# Extension-point inspection (SPEC-047) — the 4-family view over the live config
# + CapabilityRegistry. Folded INTO `arc ext` (WIRE-don't-rebuild) rather than a
# colliding new top-level `arc extensions` command.
# ---------------------------------------------------------------------------


def _inspect(args: argparse.Namespace) -> None:
    """Render the selected/available/signed state of all four extension-point families."""
    from arcagent.extension.inspect import inspect_extensions

    config, registry = _resolve_inspect_context(getattr(args, "agent", None))
    rows = [
        [r.family, r.kind, r.selected, "yes" if r.available else "no", r.signed]
        for r in inspect_extensions(config, registry, trusted_public_key=_agent_pubkey(config))
    ]
    if rows:
        _print_table(["Family", "Kind", "Selected", "Available", "Signed"], rows)
    else:
        _write("No extension-point families to report.")


def _verify_extensions(args: argparse.Namespace) -> None:
    """Report any selection/capability that would be REFUSED at load under the tier.

    Exits non-zero when a refusal is found so it is usable as a federal change-control gate.
    """
    from arcagent.extension.inspect import inspect_extensions

    config, registry = _resolve_inspect_context(getattr(args, "agent", None))
    tier = _config_tier(config)
    rows = inspect_extensions(config, registry, trusted_public_key=_agent_pubkey(config))
    refused = [r for r in rows if _would_refuse(r, tier)]
    if not refused:
        _write(f"All extension-point selections load-clean at tier {tier!r}.")
        return
    _write(f"Refusals at tier {tier!r}:")
    for r in refused:
        _write(f"  [REFUSED] {r.family}/{r.kind}: {r.selected} ({r.signed})")
    sys.exit(1)


def _would_refuse(row: object, tier: str) -> bool:
    """A row that would be refused at load: an unavailable/refused select-one, or an
    unsigned scan-many capability above personal (the loader's signature floor)."""
    signed = getattr(row, "signed", "")
    kind = getattr(row, "kind", "")
    available = getattr(row, "available", True)
    if kind == "select_one":
        return signed == "refused" or not available
    return tier in ("enterprise", "federal") and signed == "unsigned"


def _resolve_inspect_context(agent_dir: str | None) -> tuple[object, object | None]:
    """Load the config the agent runtime flat-reads + a populated CapabilityRegistry."""
    config = _load_flat_config(agent_dir)
    registry = _build_registry(Path(agent_dir).expanduser().resolve() if agent_dir else None)
    return config, registry


def _load_flat_config(agent_dir: str | None) -> object:
    """Flat-read the target arcagent.toml (matching the __main__ runtime path).

    Injects placeholder ``agent``/``llm`` when absent (a user-wide ~/.arc file omits
    them) so the real ``ArcAgentConfig`` validates — inspection reads only tier + modules.
    """
    import tomllib

    from arcagent.core.config import ArcAgentConfig

    base = Path(agent_dir).expanduser().resolve() if agent_dir else Path.home() / ".arc"
    path = base / "arcagent.toml"
    if not path.is_file():
        sys.stderr.write(f"Error: no arcagent.toml at {path}. Pass --agent DIR.\n")
        sys.exit(1)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    raw.setdefault("agent", {"name": "inspect"})
    raw.setdefault("llm", {"model": "none"})
    return ArcAgentConfig.model_validate(raw)


def _config_tier(config: object) -> str:
    try:
        return str(config.security.tier)  # type: ignore[attr-defined]
    except AttributeError:
        return "personal"


def _agent_pubkey(config: object) -> bytes | None:
    """Resolve the agent's DID public key (read-only) to PIN capability signatures.

    This is the authority the self-modification tools sign agent-authored artifacts with
    (``_runtime.sign_artifact_file`` → the agent DID key), so a wrong-key self-signed
    capability shows "unsigned" instead of a false "signed" (SPEC-047 HIGH-1). Loads the
    ``.pub`` from the identity key dir only when a DID is configured — never generates a
    key (an inspection must not mint identity). Returns None when no DID is resolvable.
    """
    from arctrust.identity import AgentIdentity

    identity_cfg = getattr(config, "identity", None)
    did = str(getattr(identity_cfg, "did", "") or "")
    key_dir = str(getattr(identity_cfg, "key_dir", "") or "")
    if not did or not key_dir:
        return None
    try:
        return AgentIdentity.load_keys(did, Path(key_dir).expanduser()).public_key
    except Exception:  # reason: no/invalid key on disk → nothing to pin (inspection best-effort)
        return None


def _build_registry(agent_root: Path | None) -> object | None:
    """Build a CapabilityRegistry over the standard scan roots (inspection posture)."""
    import asyncio

    import arcagent.builtins.capabilities as builtins_pkg
    from arcagent.capabilities.capability_loader import CapabilityLoader
    from arcagent.capabilities.capability_registry import CapabilityRegistry

    builtins_root = Path(builtins_pkg.__file__).parent
    roots: list[tuple[str, Path]] = [
        ("builtins", builtins_root),
        ("builtins-skills", builtins_root / "skills"),
    ]
    global_root = Path("~/.arc/capabilities").expanduser()
    if global_root.is_dir():
        roots.append(("global", global_root))
    if agent_root is not None:
        for name, sub in (("agent", "capabilities"), ("workspace", "workspace/capabilities")):
            path = agent_root / sub
            if path.is_dir():
                roots.append((name, path))

    registry = CapabilityRegistry()
    loader = CapabilityLoader(scan_roots=roots, registry=registry, allow_all_imports=True)
    try:
        asyncio.run(loader.scan_and_register())
    except Exception:  # reason: inspection is best-effort — degrade to select-one only
        _logger.warning("could not build capability registry for inspection", exc_info=True)
        return None
    return registry


# ---------------------------------------------------------------------------
# Argparse-based dispatcher
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc ext",
        description="Capability file management — list, create, install, validate.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    p = subs.add_parser("list", help="List discovered capability files.")
    p.add_argument(
        "--agent",
        dest="agent",
        default=None,
        help="Agent directory to include per-agent + workspace roots.",
    )

    p = subs.add_parser("create", help="Scaffold a new capability .py with a @tool template.")
    p.add_argument("name", help="Capability name.")
    p.add_argument("--dir", dest="dir", default=None, help="Output directory (default: cwd).")
    p.add_argument(
        "--global",
        dest="use_global",
        action="store_true",
        help="Write to ~/.arc/capabilities/.",
    )

    p = subs.add_parser("install", help="Install a capability .py into ~/.arc/capabilities/.")
    p.add_argument("source", help="Source .py file or directory.")

    p = subs.add_parser("validate", help="Validate a capability file.")
    p.add_argument("path", help="Path to the capability .py file.")

    p = subs.add_parser(
        "inspect", help="Show selected/available/signed state of all extension-point families."
    )
    p.add_argument("--agent", dest="agent", default=None, help="Agent dir (config + registry).")

    p = subs.add_parser(
        "verify", help="Report extension-point selections that would be refused at load."
    )
    p.add_argument("--agent", dest="agent", default=None, help="Agent dir (config + registry).")

    return parser


_SUBCOMMAND_MAP = {
    "list": _list,
    "create": _create,
    "install": _install,
    "validate": _validate,
    "inspect": _inspect,
    "verify": _verify_extensions,
}


def ext_handler(args: list[str]) -> None:
    """Top-level handler for `arc ext <sub> [args]`."""
    parser = _build_parser()

    if not args:
        parser.print_help()
        sys.exit(0)

    parsed = parser.parse_args(args)

    if parsed.subcmd is None:
        parser.print_help()
        sys.exit(0)

    fn = _SUBCOMMAND_MAP.get(parsed.subcmd)
    if fn is None:
        sys.stderr.write(f"arc ext: unknown subcommand '{parsed.subcmd}'\n")
        sys.exit(1)

    fn(parsed)
