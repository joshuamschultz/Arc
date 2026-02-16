"""Extension management CLI — `arc ext` commands."""

from __future__ import annotations

import importlib.util
import shutil
import textwrap
from pathlib import Path

import click

from arccli.formatting import click_echo, print_table

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


@click.group("ext")
def ext() -> None:
    """Extension management — list, create, install, validate."""


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@ext.command("list")
@click.option("--agent", "agent_dir", default=None, help="Agent directory to include workspace extensions.")
def ext_list(agent_dir: str | None) -> None:
    """List discovered extensions.

    Scans ~/.arcagent/extensions/ and optionally an agent's workspace/extensions/.
    """
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
        print_table(["Name", "Source", "Path", "Valid Factory"], rows)
    else:
        click_echo("No extensions found.")
        click_echo(f"  Global dir: {_GLOBAL_EXT_DIR}")
        if agent_dir:
            click_echo(f"  Agent dir:  {Path(agent_dir).expanduser().resolve() / 'workspace' / 'extensions'}")


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@ext.command("create")
@click.argument("name")
@click.option("--dir", "target_dir", default=None, help="Output directory (default: cwd).")
@click.option("--global", "use_global", is_flag=True, help="Write to ~/.arcagent/extensions/.")
def ext_create(name: str, target_dir: str | None, use_global: bool) -> None:
    """Scaffold a new extension file with boilerplate.

    Creates NAME.py with a factory function template.
    """
    if use_global:
        out_dir = _GLOBAL_EXT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
    elif target_dir:
        out_dir = Path(target_dir).expanduser().resolve()
    else:
        out_dir = Path.cwd()

    out_file = out_dir / f"{name}.py"
    if out_file.exists():
        raise click.ClickException(f"File already exists: {out_file}")

    out_file.write_text(_EXTENSION_TEMPLATE.format(name=name))
    click_echo(f"Created extension: {out_file}")
    click_echo()
    click_echo("Next steps:")
    click_echo(f"  1. Edit {out_file} to add your tools/hooks")
    click_echo(f"  2. arc ext validate {out_file}")


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


@ext.command("install")
@click.argument("source")
def ext_install(source: str) -> None:
    """Install an extension to ~/.arcagent/extensions/.

    SOURCE can be a .py file or a directory containing extension files.
    """
    src = Path(source).expanduser().resolve()
    _GLOBAL_EXT_DIR.mkdir(parents=True, exist_ok=True)

    if src.is_file():
        dest = _GLOBAL_EXT_DIR / src.name
        if dest.exists():
            raise click.ClickException(f"Already installed: {dest}")
        shutil.copy2(src, dest)
        click_echo(f"Installed: {src.name} -> {dest}")

    elif src.is_dir():
        copied = 0
        for py_file in sorted(src.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            dest = _GLOBAL_EXT_DIR / py_file.name
            if dest.exists():
                click_echo(f"  Skipped (exists): {py_file.name}")
                continue
            shutil.copy2(py_file, dest)
            click_echo(f"  Installed: {py_file.name}")
            copied += 1
        click_echo(f"\nInstalled {copied} extension(s) to {_GLOBAL_EXT_DIR}")
    else:
        raise click.ClickException(f"Source not found: {src}")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@ext.command("validate")
@click.argument("path")
def ext_validate(path: str) -> None:
    """Validate an extension file.

    Checks that the file exports an `extension()` factory function
    and can be imported without errors.
    """
    ext_path = Path(path).expanduser().resolve()
    if not ext_path.exists():
        raise click.ClickException(f"File not found: {ext_path}")
    if not ext_path.suffix == ".py":
        raise click.ClickException(f"Expected .py file: {ext_path}")

    # Try importing
    module_name = f"arcagent_ext_validate_{ext_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, ext_path)
    if spec is None or spec.loader is None:
        raise click.ClickException(f"Could not create import spec for: {ext_path}")

    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        click_echo(f"  [FAIL] Import error: {e}")
        raise SystemExit(1)

    if not hasattr(mod, "extension"):
        click_echo(f"  [FAIL] No `extension()` factory function found")
        raise SystemExit(1)

    factory = getattr(mod, "extension")
    if not callable(factory):
        click_echo(f"  [FAIL] `extension` is not callable")
        raise SystemExit(1)

    click_echo(f"  [OK] {ext_path.name}")
    click_echo(f"       Factory: extension()")
    click_echo(f"       Path:    {ext_path}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _check_has_factory(py_file: Path) -> bool:
    """Quick check if a .py file contains an `extension` function."""
    try:
        content = py_file.read_text(encoding="utf-8")
        return "def extension(" in content
    except Exception:
        return False
