"""Plain CommandDef handlers for the `arc ext` subcommand group.

T1.1.5 migration: replaces the legacy Click-based dispatch in registry.py.
Each function is a direct translation of the corresponding Click command body
in arccli.ext, with Click-specific calls replaced with stdlib equivalents.

Layer contract: this module may import from arcagent.
It MUST NOT import click or arccli.main_legacy.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path

_GLOBAL_EXT_DIR = Path.home() / ".arcagent" / "extensions"

_EXTENSION_TEMPLATE = '''\
"""Extension: {name}

Registers tools and event hooks with ArcAgent.
"""

from __future__ import annotations


def extension(api):
    """Factory function called by ExtensionLoader.

    Parameters
    ----------
    api : ExtensionAPI
        Provides register_tool(), on(), and workspace property.
    """
    from arcrun import Tool, ToolContext

    async def hello(params: dict, ctx: ToolContext) -> str:
        """Example tool — say hello."""
        return f"Hello from {name}!"

    api.register_tool(
        Tool(
            name="{name}_hello",
            description="Say hello from the {name} extension.",
            input_schema={{
                "type": "object",
                "properties": {{}},
            }},
            execute=hello,
        )
    )
'''


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write(msg: str = "") -> None:
    """Write a line to stdout."""
    sys.stdout.write(msg + "\n")


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print a table with headers."""
    try:
        from arccli.formatting import print_table

        print_table(headers, rows)
    except ImportError:
        sys.stdout.write("  " + "  ".join(headers) + "\n")
        for row in rows:
            sys.stdout.write("  " + "  ".join(row) + "\n")


def _check_has_factory(py_file: Path) -> bool:
    """Quick check if a .py file contains an `extension` function."""
    try:
        content = py_file.read_text(encoding="utf-8")
        return "def extension(" in content
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _list(args: argparse.Namespace) -> None:
    """List discovered extensions."""
    agent_dir: str | None = getattr(args, "agent", None)
    dirs_to_scan: list[tuple[str, Path]] = []

    if _GLOBAL_EXT_DIR.is_dir():
        dirs_to_scan.append(("global", _GLOBAL_EXT_DIR))

    if agent_dir:
        ws_ext = Path(agent_dir).expanduser().resolve() / "workspace" / "extensions"
        if ws_ext.is_dir():
            dirs_to_scan.append(("workspace", ws_ext))

    rows: list[list[str]] = []
    for source, directory in dirs_to_scan:
        for py_file in sorted(directory.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            name = py_file.stem
            has_factory = _check_has_factory(py_file)
            rows.append([name, source, str(py_file), "yes" if has_factory else "no"])

    if rows:
        _print_table(["Name", "Source", "Path", "Valid Factory"], rows)
    else:
        _write("No extensions found.")
        _write(f"  Global dir: {_GLOBAL_EXT_DIR}")
        if agent_dir:
            agent_ext = Path(agent_dir).expanduser().resolve() / "workspace" / "extensions"
            _write(f"  Agent dir:  {agent_ext}")


def _create(args: argparse.Namespace) -> None:
    """Scaffold a new extension file with boilerplate."""
    name: str = args.name
    target_dir: str | None = getattr(args, "dir", None)
    use_global: bool = getattr(args, "use_global", False)

    if use_global:
        out_dir = _GLOBAL_EXT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
    elif target_dir:
        out_dir = Path(target_dir).expanduser().resolve()
    else:
        out_dir = Path.cwd()

    out_file = out_dir / f"{name}.py"
    if out_file.exists():
        sys.stderr.write(f"Error: File already exists: {out_file}\n")
        sys.exit(1)

    out_file.write_text(_EXTENSION_TEMPLATE.format(name=name))
    _write(f"Created extension: {out_file}")
    _write()
    _write("Next steps:")
    _write(f"  1. Edit {out_file} to add your tools/hooks")
    _write(f"  2. arc ext validate {out_file}")


def _install(args: argparse.Namespace) -> None:
    """Install an extension to ~/.arcagent/extensions/."""
    source: str = args.source
    src = Path(source).expanduser().resolve()
    _GLOBAL_EXT_DIR.mkdir(parents=True, exist_ok=True)

    if src.is_file():
        dest = _GLOBAL_EXT_DIR / src.name
        if dest.exists():
            sys.stderr.write(f"Error: Already installed: {dest}\n")
            sys.exit(1)
        shutil.copy2(src, dest)
        _write(f"Installed: {src.name} -> {dest}")

    elif src.is_dir():
        copied = 0
        for py_file in sorted(src.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            dest = _GLOBAL_EXT_DIR / py_file.name
            if dest.exists():
                _write(f"  Skipped (exists): {py_file.name}")
                continue
            shutil.copy2(py_file, dest)
            _write(f"  Installed: {py_file.name}")
            copied += 1
        _write(f"\nInstalled {copied} extension(s) to {_GLOBAL_EXT_DIR}")
    else:
        sys.stderr.write(f"Error: Source not found: {src}\n")
        sys.exit(1)


def _validate(args: argparse.Namespace) -> None:
    """Validate an extension file."""
    path: str = args.path
    ext_path = Path(path).expanduser().resolve()
    if not ext_path.exists():
        sys.stderr.write(f"Error: File not found: {ext_path}\n")
        sys.exit(1)
    if ext_path.suffix != ".py":
        sys.stderr.write(f"Error: Expected .py file: {ext_path}\n")
        sys.exit(1)

    module_name = f"arcagent_ext_validate_{ext_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, ext_path)
    if spec is None or spec.loader is None:
        sys.stderr.write(f"Error: Could not create import spec for: {ext_path}\n")
        sys.exit(1)

    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        _write(f"  [FAIL] Import error: {e}")
        sys.exit(1)

    if not hasattr(mod, "extension"):
        _write("  [FAIL] No `extension()` factory function found")
        sys.exit(1)

    factory = mod.extension
    if not callable(factory):
        _write("  [FAIL] `extension` is not callable")
        sys.exit(1)

    _write(f"  [OK] {ext_path.name}")
    _write("       Factory: extension()")
    _write(f"       Path:    {ext_path}")


# ---------------------------------------------------------------------------
# Argparse-based dispatcher
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for `arc ext <sub> [args]`."""
    parser = argparse.ArgumentParser(
        prog="arc ext",
        description="Extension management — list, create, install, validate.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    # list
    p = subs.add_parser("list", help="List discovered extensions.")
    p.add_argument(
        "--agent", dest="agent", default=None,
        help="Agent directory to include workspace extensions."
    )

    # create
    p = subs.add_parser("create", help="Scaffold a new extension file with boilerplate.")
    p.add_argument("name", help="Extension name.")
    p.add_argument("--dir", dest="dir", default=None, help="Output directory (default: cwd).")
    p.add_argument(
        "--global", dest="use_global", action="store_true",
        help="Write to ~/.arcagent/extensions/."
    )

    # install
    p = subs.add_parser("install", help="Install an extension to ~/.arcagent/extensions/.")
    p.add_argument("source", help="Source .py file or directory.")

    # validate
    p = subs.add_parser("validate", help="Validate an extension file.")
    p.add_argument("path", help="Path to the extension .py file.")

    return parser


_SUBCOMMAND_MAP = {
    "list": _list,
    "create": _create,
    "install": _install,
    "validate": _validate,
}


def ext_handler(args: list[str]) -> None:
    """Top-level handler for `arc ext <sub> [args]`.

    Called by arccli.commands.registry when the user runs `arc ext ...`.
    """
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
