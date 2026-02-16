"""Extension system — discover, load, and sandbox Python extensions.

Extensions are Python files with a factory function that receives
an ExtensionAPI instance for registering tools and event hooks.
Uses importlib exclusively — no exec(), eval(), or dynamic compilation.
"""

from __future__ import annotations

import builtins
import contextlib
import importlib
import importlib.metadata
import importlib.util
import logging
import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcagent.core.config import ExtensionConfig
from arcagent.core.module_bus import EventContext, ModuleBus
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry

_logger = logging.getLogger("arcagent.extensions")

_EXTENSION_SOURCE_PREFIX = "extension:"
_WORKSPACE_TOOL_SOURCE_PREFIX = "workspace_tool:"
_EXTENSION_HOOK_PREFIX = "ext:"

_VALID_SANDBOX_MODES = {"workspace", "paths", "strict"}


@dataclass
class ExtensionManifest:
    """Metadata about a loaded extension."""

    name: str
    source: str
    sandbox_mode: str
    tools_registered: list[str] = field(default_factory=list)
    hooks_registered: list[str] = field(default_factory=list)
    load_time_ms: float = 0.0


class ExtensionAPI:
    """API surface exposed to extension factory functions.

    Provides register_tool(), on(), and workspace access.
    All registrations are tracked for hot reload cleanup.
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        bus: ModuleBus,
        workspace: Path,
        sandbox_mode: str,
        extension_name: str,
        source_prefix: str = _EXTENSION_SOURCE_PREFIX,
    ) -> None:
        self._tool_registry = tool_registry
        self._bus = bus
        self._workspace = workspace
        self._sandbox_mode = sandbox_mode
        self._extension_name = extension_name
        self._source_prefix = source_prefix
        self._tools_registered: list[str] = []
        self._hooks_registered: list[str] = []

    def register_tool(self, tool: RegisteredTool) -> None:
        """Register a tool via the tool registry.

        Tags the tool source for cleanup during hot reload.
        Accepts both RegisteredTool and arcrun.Tool objects.
        """
        # Convert arcrun.Tool to RegisteredTool if needed
        if not hasattr(tool, 'source') or not hasattr(tool, 'transport'):
            from arcagent.core.tool_registry import RegisteredTool as RT, ToolTransport
            tool = RT(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                transport=ToolTransport.NATIVE,
                execute=tool.execute,
                timeout_seconds=getattr(tool, 'timeout_seconds', None),
                source=f"{self._source_prefix}{self._extension_name}",
            )
        # Ensure source is tagged for cleanup with the correct prefix
        elif not tool.source.startswith(self._source_prefix):
            tool = RegisteredTool(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                transport=tool.transport,
                execute=tool.execute,
                timeout_seconds=tool.timeout_seconds,
                source=f"{self._source_prefix}{self._extension_name}",
            )
        self._tool_registry.register(tool)
        self._tools_registered.append(tool.name)

    def on(
        self,
        event: str,
        handler: Callable[[EventContext], Awaitable[None]],
        priority: int = 100,
    ) -> None:
        """Subscribe to a Module Bus event.

        Tags the handler for cleanup during hot reload.
        """
        module_name = f"{_EXTENSION_HOOK_PREFIX}{self._extension_name}"
        self._bus.subscribe(
            event=event,
            handler=handler,
            priority=priority,
            module_name=module_name,
        )
        self._hooks_registered.append(event)

    @property
    def workspace(self) -> Path:
        """Read-only access to workspace path."""
        return self._workspace


class ExtensionLoader:
    """Discover, load, and manage extensions."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        bus: ModuleBus,
        telemetry: Any,
        config: ExtensionConfig,
    ) -> None:
        self._tool_registry = tool_registry
        self._bus = bus
        self._telemetry = telemetry
        self._config = config
        self._manifests: list[ExtensionManifest] = []

    @property
    def manifests(self) -> list[ExtensionManifest]:
        """All loaded extension manifests."""
        return list(self._manifests)

    async def discover_and_load(
        self, workspace: Path, global_dir: Path
    ) -> list[ExtensionManifest]:
        """Discover and load extensions from all sources.

        Discovery order:
        1. workspace/extensions/*.py
        2. workspace/tools/*.py (agent-built tools)
        3. global_dir/*.py
        4. Config-specified paths
        5. Entry points
        """
        self._manifests.clear()

        scan_dirs: list[tuple[Path, str]] = []

        ws_ext = workspace / "extensions"
        if ws_ext.is_dir():
            scan_dirs.append((ws_ext, _EXTENSION_SOURCE_PREFIX))

        ws_tools = workspace / self._config.workspace_tools_dir
        if ws_tools.is_dir():
            scan_dirs.append((ws_tools, _WORKSPACE_TOOL_SOURCE_PREFIX))

        if global_dir.is_dir():
            scan_dirs.append((global_dir, _EXTENSION_SOURCE_PREFIX))

        for extra_path in self._config.paths:
            p = Path(extra_path).expanduser()
            if p.is_dir():
                scan_dirs.append((p, _EXTENSION_SOURCE_PREFIX))

        for directory, prefix in scan_dirs:
            for py_file in sorted(directory.glob("*.py")):
                if py_file.name.startswith("_"):
                    continue
                self._load_extension(py_file, workspace, prefix)

        # 4. Entry point discovery
        self._load_entry_points(workspace)

        _logger.info(
            "Loaded %d extensions (%d tools registered)",
            len(self._manifests),
            sum(len(m.tools_registered) for m in self._manifests),
        )
        return list(self._manifests)

    def clear(
        self,
        tool_registry: ToolRegistry,
        bus: ModuleBus,
    ) -> None:
        """Remove all extension-registered tools and hooks.

        Called during hot reload before re-discovery.
        """
        # Remove extension and workspace tool-registered tools from registry
        ext_tools = [
            name
            for name, tool in tool_registry.tools.items()
            if tool.source.startswith((_EXTENSION_SOURCE_PREFIX, _WORKSPACE_TOOL_SOURCE_PREFIX))
        ]
        for name in ext_tools:
            del tool_registry.tools[name]

        # Remove extension hooks from bus via public API
        bus.unsubscribe_by_module_prefix(_EXTENSION_HOOK_PREFIX)

        # Invalidate import caches for re-import
        importlib.invalidate_caches()

        self._manifests.clear()
        _logger.info("Cleared all extensions")

    def _resolve_sandbox_config(self, ext_name: str) -> tuple[str, list[Path]]:
        """Resolve sandbox mode and allowed paths for an extension.

        Returns (sandbox_mode, allowed_paths). Falls back to 'workspace'
        with no extra paths if not configured.
        """
        ext_entry = self._config.extensions.get(ext_name)
        mode = ext_entry.sandbox_mode if ext_entry else "workspace"
        if mode not in _VALID_SANDBOX_MODES:
            _logger.warning(
                "Extension %s has unsupported sandbox_mode '%s', falling back to 'workspace'",
                ext_name,
                mode,
            )
            mode = "workspace"
        allowed: list[Path] = []
        if ext_entry and ext_entry.allowed_paths:
            allowed = [Path(p).resolve() for p in ext_entry.allowed_paths]
        return mode, allowed

    def _run_factory(
        self,
        factory: Any,
        ext_name: str,
        source: str,
        sandbox_mode: str,
        workspace: Path,
        source_prefix: str = _EXTENSION_SOURCE_PREFIX,
        sandbox_allowed_paths: list[Path] | None = None,
    ) -> ExtensionManifest | None:
        """Run factory function, build manifest. Shared by file and entry point loading."""
        start = time.monotonic()

        api = ExtensionAPI(
            tool_registry=self._tool_registry,
            bus=self._bus,
            workspace=workspace,
            sandbox_mode=sandbox_mode,
            extension_name=ext_name,
            source_prefix=source_prefix,
        )

        try:
            if sandbox_mode == "strict":
                with _strict_sandbox(workspace):
                    factory(api)
            elif sandbox_mode == "paths":
                with _paths_sandbox(workspace, sandbox_allowed_paths or []):
                    factory(api)
            else:
                factory(api)
        except Exception:
            _logger.exception("Extension factory failed: %s", ext_name)
            self._telemetry.audit_event(
                "extension.load_failed",
                {"name": ext_name, "source": source, "phase": "factory"},
            )
            return None

        elapsed_ms = (time.monotonic() - start) * 1000
        manifest = ExtensionManifest(
            name=ext_name,
            source=source,
            sandbox_mode=sandbox_mode,
            tools_registered=list(api._tools_registered),
            hooks_registered=list(api._hooks_registered),
            load_time_ms=elapsed_ms,
        )
        self._manifests.append(manifest)

        self._telemetry.audit_event(
            "extension.loaded",
            {
                "name": ext_name,
                "source": source,
                "sandbox_mode": sandbox_mode,
                "tools_registered": manifest.tools_registered,
                "hooks_registered": manifest.hooks_registered,
                "load_time_ms": round(elapsed_ms, 2),
            },
        )
        _logger.info(
            "Loaded extension %s: %d tools, %d hooks (%.1fms)",
            ext_name,
            len(manifest.tools_registered),
            len(manifest.hooks_registered),
            elapsed_ms,
        )
        return manifest

    def _load_extension(
        self,
        py_file: Path,
        workspace: Path,
        source_prefix: str = _EXTENSION_SOURCE_PREFIX,
    ) -> None:
        """Load a single extension file."""
        ext_name = py_file.stem

        # Check if disabled
        ext_entry = self._config.extensions.get(ext_name)
        if ext_entry and not ext_entry.enabled:
            _logger.info("Extension %s is disabled, skipping", ext_name)
            return

        sandbox_mode, sandbox_allowed = self._resolve_sandbox_config(ext_name)

        try:
            module = self._import_file(py_file, ext_name)
        except Exception:
            _logger.exception("Failed to import extension: %s", ext_name)
            self._telemetry.audit_event(
                "extension.load_failed",
                {"name": ext_name, "source": str(py_file), "phase": "import"},
            )
            return

        factory = getattr(module, "extension", None)
        if factory is None:
            _logger.warning(
                "Extension %s has no 'extension' factory function, skipping",
                ext_name,
            )
            return

        self._run_factory(
            factory, ext_name, str(py_file), sandbox_mode, workspace,
            source_prefix, sandbox_allowed,
        )

    def _load_entry_points(self, workspace: Path) -> None:
        """Load extensions discovered via importlib.metadata entry points.

        Each entry point must resolve to a factory function matching
        the standard extension(api) signature. Respects per-extension
        config sandbox_mode if configured.
        """
        for ep in _discover_entry_points():
            ext_name = ep.name
            source = f"entry_point:{ext_name}"

            try:
                factory = ep.load()
            except Exception:
                _logger.exception("Failed to load entry point extension: %s", ext_name)
                self._telemetry.audit_event(
                    "extension.load_failed",
                    {"name": ext_name, "source": source, "phase": "import"},
                )
                continue

            sandbox_mode, sandbox_allowed = self._resolve_sandbox_config(ext_name)
            self._run_factory(
                factory, ext_name, source, sandbox_mode, workspace,
                _EXTENSION_SOURCE_PREFIX, sandbox_allowed,
            )

    @staticmethod
    def _import_file(py_file: Path, module_name: str) -> Any:
        """Import a Python file as a module via importlib.

        Uses importlib.util.spec_from_file_location to load
        the file without exec/eval.
        """
        # Create a unique module name to avoid collisions
        full_name = f"arcagent_ext_{module_name}"

        # Remove from sys.modules if already loaded (for hot reload)
        sys.modules.pop(full_name, None)

        spec = importlib.util.spec_from_file_location(full_name, py_file)
        if spec is None or spec.loader is None:
            msg = f"Cannot create module spec for: {py_file}"
            raise ImportError(msg)

        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
        return module


@contextlib.contextmanager
def _filesystem_sandbox(boundaries: list[str]) -> Iterator[None]:
    """Restrict filesystem access to paths within boundary directories.

    Patches builtins.open and Path.read_text/read_bytes/write_text/write_bytes
    to only permit access within the specified boundary paths.
    All patches are restored in the finally block.
    """
    original_open = builtins.open
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes
    original_write_text = Path.write_text
    original_write_bytes = Path.write_bytes

    def _check(file: Any) -> None:
        resolved = str(Path(str(file)).resolve())
        if not any(resolved.startswith(b) for b in boundaries):
            raise PermissionError(f"Sandbox: access denied to {resolved}")

    def _restricted_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        _check(file)
        return original_open(file, *args, **kwargs)

    def _restricted_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        _check(self)
        return original_read_text(self, *args, **kwargs)

    def _restricted_read_bytes(self: Path, *args: Any, **kwargs: Any) -> bytes:
        _check(self)
        return original_read_bytes(self, *args, **kwargs)

    def _restricted_write_text(self: Path, *args: Any, **kwargs: Any) -> int:
        _check(self)
        return original_write_text(self, *args, **kwargs)

    def _restricted_write_bytes(self: Path, *args: Any, **kwargs: Any) -> int:
        _check(self)
        return original_write_bytes(self, *args, **kwargs)

    try:
        builtins.open = _restricted_open
        Path.read_text = _restricted_read_text  # type: ignore[method-assign]
        Path.read_bytes = _restricted_read_bytes  # type: ignore[method-assign]
        Path.write_text = _restricted_write_text  # type: ignore[method-assign]
        Path.write_bytes = _restricted_write_bytes  # type: ignore[method-assign]
        yield
    finally:
        builtins.open = original_open
        Path.read_text = original_read_text  # type: ignore[method-assign]
        Path.read_bytes = original_read_bytes  # type: ignore[method-assign]
        Path.write_text = original_write_text  # type: ignore[method-assign]
        Path.write_bytes = original_write_bytes  # type: ignore[method-assign]


@contextlib.contextmanager
def _paths_sandbox(workspace: Path, allowed_paths: list[Path]) -> Iterator[None]:
    """Restrict filesystem access to workspace + allowed_paths.

    Unlike strict mode, this does NOT block subprocess or network.
    """
    boundaries = [str(workspace.resolve())]
    boundaries.extend(str(p.resolve()) for p in allowed_paths)
    with _filesystem_sandbox(boundaries):
        yield


@contextlib.contextmanager
def _strict_sandbox(workspace: Path) -> Iterator[None]:
    """Restrict filesystem, subprocess, and network access during factory call.

    Composes _filesystem_sandbox for file access with additional blocks on
    subprocess execution and network access.

    Note: This is a best-effort Phase 1 sandbox. Process-level isolation
    (seccomp/landlock/Firecracker) is needed for real security (see ADR-002).
    """
    original_run = subprocess.run
    original_popen = subprocess.Popen
    original_os_system = os.system
    original_os_popen = os.popen
    original_urlopen = urllib.request.urlopen

    def _blocked_subprocess(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("Strict sandbox: subprocess execution denied")

    class _BlockedPopen:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise PermissionError("Strict sandbox: subprocess execution denied")

    def _blocked_network(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("Strict sandbox: network access denied")

    try:
        subprocess.run = _blocked_subprocess
        subprocess.Popen = _BlockedPopen  # type: ignore[misc,assignment]
        os.system = _blocked_subprocess
        os.popen = _blocked_subprocess
        urllib.request.urlopen = _blocked_network
        with _filesystem_sandbox([str(workspace.resolve())]):
            yield
    finally:
        subprocess.run = original_run
        subprocess.Popen = original_popen  # type: ignore[misc]
        os.system = original_os_system
        os.popen = original_os_popen
        urllib.request.urlopen = original_urlopen


def _discover_entry_points() -> list[Any]:
    """Discover extensions registered via importlib.metadata entry points.

    Returns entry points from the ``arcagent.extensions`` group.
    Isolated as a function to allow test mocking.
    """
    eps = importlib.metadata.entry_points()
    if hasattr(eps, "select"):
        return list(eps.select(group="arcagent.extensions"))
    # Python 3.9-3.11 compat: eps is a dict
    return list(eps.get("arcagent.extensions", []))  # type: ignore[attr-defined]
