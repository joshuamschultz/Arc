"""`/api/agents/{id}/tools` route handler + tool discovery."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from arcgateway import fs_reader
from arcgateway.fs_reader import FileTooLargeError, PathTraversalError
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.routes.agent_detail._common import (
    _CALLER_DID,
    _agent_root,
    _compute_write_target,
    _read_text_or_empty,
)
from arcui.schemas import ErrorResponse, ToolDetailResponse, ToolsResponse

# Transports whose files live under the agent root and are safe to edit via
# the shared `PUT /files/read` route — mirrors U5's editable-source-root
# check. Builtins, arcagent-module tools, and the operator-curated global
# capabilities root (outside the agent root) are always read-only.
_EDITABLE_TOOL_TRANSPORTS = frozenset({"workspace", "agent_dir", "capability"})

# Classification fallbacks for builtin tools whose source omits the kwarg.
# The loader reads these from the @tool decorator; the few that don't set
# ``classification`` get a sensible default here so the UI isn't blank.
_BUILTIN_CLASSIFICATION: dict[str, str] = {
    "read": "read_only",
    "find": "read_only",
    "grep": "read_only",
    "ls": "read_only",
    "write": "state_modifying",
    "edit": "state_modifying",
    "bash": "external_effect",
}
# Capture name + every keyword argument across the (...) block. The body is
# matched non-greedily and anchored on the ``)`` + ``def`` terminator, so a
# literal ``@`` inside a description (e.g. "Author a new @tool file") does not
# truncate the match.
_TOOL_BLOCK_RE = re.compile(
    r"@tool\s*\(\s*(?P<body>.*?)\)\s*\n\s*async\s+def|"
    r"@tool\s*\(\s*(?P<body2>.*?)\)\s*\n\s*def",
    re.DOTALL,
)
# Capability tools use ToolMetadata(...) assignment instead of @tool().
_TOOL_META_RE = re.compile(
    r"ToolMetadata\s*\(\s*(?P<body>[^)]*(?:\([^)]*\)[^)]*)*)\)",
    re.DOTALL,
)
_KW_NAME_RE = re.compile(r'name\s*=\s*["\']([^"\']+)["\']')
_KW_CLASS_RE = re.compile(r'classification\s*=\s*["\']([^"\']+)["\']')
_KW_DESC_RE = re.compile(r'description\s*=\s*(?P<q>["\']{1,3})(?P<text>.+?)(?P=q)', re.DOTALL)


def _arcagent_pkg_dir() -> Path | None:
    """Locate the installed ``arcagent`` package directory on disk.

    Uses ``importlib.util.find_spec`` rather than ``import arcagent`` so this
    module preserves the SPEC-023 §2.2 boundary that arcui does not import
    arcagent. The spec only locates the package's filesystem path; it does
    not execute arcagent code.
    """
    import importlib.util

    try:
        spec = importlib.util.find_spec("arcagent")
        if spec is not None and spec.origin is not None:
            return Path(spec.origin).parent
    except (ImportError, ValueError):
        pass
    return None


def _arcagent_modules_dir() -> Path:
    """Locate ``arcagent/modules/`` on disk so we can scan capabilities files
    for `@tool(...)` declarations.
    """
    pkg = _arcagent_pkg_dir()
    if pkg is not None:
        return pkg / "modules"
    return Path(__file__).resolve().parents[5] / "arcagent/src/arcagent/modules"


def _parse_tool_blocks(text: str) -> list[dict[str, str]]:
    """Pull (name, classification, description) from @tool(...) and
    ToolMetadata(...) blocks in a Python source file. Robust to
    single/double/triple quotes and line wraps inside the description.
    Missing fields surface as ''."""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    def _extract(body: str) -> None:
        name_m = _KW_NAME_RE.search(body)
        if not name_m:
            return
        name = name_m.group(1)
        if name in seen:
            return
        seen.add(name)
        cls_m = _KW_CLASS_RE.search(body)
        desc_m = _KW_DESC_RE.search(body)
        rows.append(
            {
                "name": name,
                "classification": cls_m.group(1) if cls_m else "",
                "description": desc_m.group("text").strip() if desc_m else "",
            }
        )

    for m in _TOOL_BLOCK_RE.finditer(text):
        _extract(m.group("body") or m.group("body2") or "")
    for m in _TOOL_META_RE.finditer(text):
        _extract(m.group("body"))
    return rows


def _collect_module_tools(modules: dict[str, Any]) -> list[dict[str, str]]:
    """Walk every `[modules.X]` section regardless of `enabled` flag and
    surface its tools. Disabled modules are tagged ``status="inactive"``
    so the UI can show greyed-out rows; enabled stay ``"allow"``.

    Returns [] silently when capabilities.py is missing so the route
    stays robust to a partial install or in-tree refactor."""
    out: list[dict[str, str]] = []
    base = _arcagent_modules_dir()
    if not base.is_dir():
        return out
    for mod_name, entry in modules.items():
        enabled = isinstance(entry, dict) and entry.get("enabled", True)
        cap_path = base / mod_name / "capabilities.py"
        if not cap_path.is_file():
            continue
        try:
            text = cap_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for row in _parse_tool_blocks(text):
            out.append(
                {
                    "name": row["name"],
                    "transport": f"module:{mod_name}",
                    "classification": row["classification"],
                    "description": row["description"],
                    "status": "allow" if enabled else "inactive",
                }
            )
    return out


def _collect_builtin_tools() -> list[dict[str, str]]:
    """Scan the arcagent builtins capabilities dir for `@tool(...)` files.

    Mirrors the loader's first scan root (``builtins/capabilities/*.py``) so the
    UI surfaces every builtin tool — read/write/edit/bash/find/grep/ls/reload
    plus the self-modification tools (create_tool, create_skill, update_tool,
    update_skill) — instead of a hand-maintained subset that drifts.

    Falls back to the static classification map when a builtin's source omits
    the ``classification`` kwarg.
    """
    out: list[dict[str, str]] = []
    pkg = _arcagent_pkg_dir()
    if pkg is None:
        return out
    builtins_dir = pkg / "builtins" / "capabilities"
    if not builtins_dir.is_dir():
        return out
    for child in sorted(builtins_dir.glob("*.py")):
        if child.name.startswith("_"):
            continue
        try:
            text = child.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for row in _parse_tool_blocks(text):
            name = row["name"]
            out.append(
                {
                    "name": name,
                    "transport": "builtin",
                    "classification": row.get("classification")
                    or _BUILTIN_CLASSIFICATION.get(name, ""),
                    "description": row.get("description") or "",
                }
            )
    return out


def _stem_row(child: Path, transport: str) -> dict[str, str]:
    """Metadata-less fallback row for a tool file with no ``@tool(...)`` block."""
    return {"name": child.stem, "transport": transport, "classification": "", "description": ""}


def _disk_tool_roots(agent_root: Path) -> list[tuple[Path, str]]:
    """Directories scanned for on-disk tool `.py` files, mirroring the loader's
    tool roots. Shared by `_collect_disk_tools` (list view) and
    `_locate_disk_tool_file` (U6 detail lookup) so both walk the same roots in
    the same precedence order.

    Locations checked (each optional):
      - ~/.arc/capabilities/*.py                 (global, operator-curated)
      - team/<agent>/tools/*.py                  (agent-shipped Python tools)
      - team/<agent>/workspace/tools/*.py        (agent-authored runtime tools)
      - team/<agent>/extensions/*                (extension modules)
      - team/<agent>/capabilities/*.py           (per-agent capabilities)
      - team/<agent>/workspace/capabilities/*.py (agent-authored at runtime)
    """
    return [
        # Global capabilities root — the loader scans ~/.arc/capabilities/.
        (Path.home() / ".arc" / "capabilities", "global"),
        (agent_root / "tools", "agent_dir"),
        (agent_root / "workspace" / "tools", "workspace"),
        (agent_root / "extensions", "extension"),
        (agent_root / "capabilities", "capability"),
        # Where create_tool writes agent-authored tools at runtime — this is the
        # path the UI must scan so newly created tools appear on refresh.
        (agent_root / "workspace" / "capabilities", "workspace"),
    ]


def _collect_disk_tools(agent_root: Path) -> list[dict[str, str]]:
    """Scan agent-local + workspace tool directories for .py modules and
    parse `@tool(...)` blocks for name/classification/description metadata.

    Falls back to the file stem when there's no `@tool(...)` decorator —
    the file is still a tool surface, just one without rich metadata.
    Agent-created tools that DO use the decorator inherit the same
    classification surface as built-in/module tools.
    """
    out: list[dict[str, str]] = []
    for path, transport in _disk_tool_roots(agent_root):
        if not path.is_dir():
            continue
        for child in sorted(path.iterdir()):
            if child.name.startswith("_") or child.name.startswith("."):
                continue
            if child.is_file() and child.suffix == ".py":
                try:
                    text = child.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    out.append(_stem_row(child, transport))
                    continue
                blocks = _parse_tool_blocks(text)
                if blocks:
                    for row in blocks:
                        out.append(
                            {
                                "name": row["name"],
                                "transport": transport,
                                "classification": row.get("classification") or "",
                                "description": row.get("description") or "",
                            }
                        )
                else:
                    out.append(_stem_row(child, transport))
            # Subdirs are NOT tools. The loader treats a capabilities subdir as a
            # skill (when it holds a SKILL.md) or ignores it — never as a tool.
            # ``skills/`` and each authored skill folder must not leak in here.
    return out


def _load_tool_policy(
    agent_id: str, agent_root: Path
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Read ``[tools.policy]`` allow/deny lists and ``[modules]`` from arcagent.toml.

    Returns ``(allowlist, denylist, enabled_modules)``; defaults to empty when
    the file is absent, unreadable, or malformed — the tools route stays robust
    to a partial install.
    """
    allowlist: list[str] = []
    denylist: list[str] = []
    enabled_modules: dict[str, Any] = {}
    try:
        content = fs_reader.read_file(
            scope="agent",
            agent_id=agent_id,
            agent_root=agent_root,
            rel_path="arcagent.toml",
            caller_did=_CALLER_DID,
        )
        cfg = tomllib.loads(content.content)
        policy = (
            cfg.get("tools", {}).get("policy", {}) if isinstance(cfg.get("tools"), dict) else {}
        )
        if isinstance(policy, dict):
            allow = policy.get("allow")
            deny = policy.get("deny")
            if isinstance(allow, list):
                allowlist = [str(x) for x in allow]
            if isinstance(deny, list):
                denylist = [str(x) for x in deny]
        if isinstance(cfg.get("modules"), dict):
            enabled_modules = cfg["modules"]
    except (FileNotFoundError, PathTraversalError, FileTooLargeError, tomllib.TOMLDecodeError):
        pass
    return allowlist, denylist, enabled_modules


async def get_tools(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )

    # Live registration — only available when the agent is connected.
    registry = request.app.state.agent_registry
    entry = registry.get(agent_id)
    live_tools: list[str] = list(entry.registration.tools) if entry is not None else []

    allowlist, denylist, enabled_modules = _load_tool_policy(agent_id, agent_root)

    # Build a single deduplicated tool list spanning all sources. Order:
    # 1) live registry → 2) builtins → 3) module-derived → 4) disk → 5) policy
    seen: dict[str, dict[str, Any]] = {}

    def _add(name: str, **fields: Any) -> None:
        if not name or name in seen:
            return
        row = {
            "name": name,
            "transport": "",
            "classification": "",
            "description": "",
            "status": "allow",
        }
        row.update({k: v for k, v in fields.items() if v not in (None, "")})
        if name in denylist:
            row["status"] = "deny"
        seen[name] = row

    for t in live_tools:
        _add(t, transport="registered")
    for row in _collect_builtin_tools():
        _add(
            row["name"],
            transport="builtin",
            classification=row.get("classification") or "",
            description=row.get("description") or "",
        )
    for row in _collect_module_tools(enabled_modules):
        _add(
            row["name"],
            transport=row["transport"],
            classification=row.get("classification") or "",
            description=row.get("description") or "",
            status=row.get("status") or "allow",
        )
    for row in _collect_disk_tools(agent_root):
        _add(row["name"], transport=row["transport"])
    for t in allowlist:
        _add(t, transport="config")

    tools = list(seen.values())

    return JSONResponse(
        ToolsResponse(
            tools=tools,
            allowlist=allowlist,
            denylist=denylist,
        ).model_dump(mode="json")
    )


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _locate_disk_tool_file(agent_root: Path, tool_name: str) -> tuple[Path, str] | None:
    """Find the on-disk `.py` file whose `@tool(...)` block defines `tool_name`.

    Walks `_disk_tool_roots` in the same precedence order `_collect_disk_tools`
    uses for its first-match-wins dedup, so the file returned here is the same
    one that wins the Tools tab's list row.
    """
    for path, transport in _disk_tool_roots(agent_root):
        if not path.is_dir():
            continue
        for child in sorted(path.iterdir()):
            if child.name.startswith("_") or child.name.startswith("."):
                continue
            if not (child.is_file() and child.suffix == ".py"):
                continue
            text = _read_text_or_empty(child)
            if any(row["name"] == tool_name for row in _parse_tool_blocks(text)):
                return child, transport
    return None


def _locate_builtin_tool_file(tool_name: str) -> Path | None:
    """Find the builtin capabilities file whose `@tool(...)` block defines
    `tool_name`, mirroring `_collect_builtin_tools`'s scan but returning the
    source file instead of a metadata row."""
    pkg = _arcagent_pkg_dir()
    if pkg is None:
        return None
    builtins_dir = pkg / "builtins" / "capabilities"
    if not builtins_dir.is_dir():
        return None
    for child in sorted(builtins_dir.glob("*.py")):
        if child.name.startswith("_"):
            continue
        text = _read_text_or_empty(child)
        if any(row["name"] == tool_name for row in _parse_tool_blocks(text)):
            return child
    return None


def _tool_detail_payload(
    *,
    name: str,
    transport: str,
    classification: str,
    description: str,
    source_path: Path,
    editable: bool,
    agent_root: Path,
) -> ToolDetailResponse:
    """Assemble the response, computing the write target only when editable."""
    write_root: str | None = None
    write_path: str | None = None
    if editable:
        write_root, write_path = _compute_write_target(agent_root, source_path)
        editable = write_root is not None
    return ToolDetailResponse(
        name=name,
        transport=transport,
        classification=classification,
        description=description,
        source_path=str(source_path),
        content=_read_text_or_empty(source_path),
        editable=editable,
        write_root=write_root,
        write_path=write_path,
    )


async def get_tool_detail(request: Request) -> JSONResponse:
    """GET .../tools/{tool_name}/detail — tool source + edit target (U6).

    Resolution order: 1) on-disk tool directories (agent/workspace-authored —
    the same roots `_collect_disk_tools` scans), 2) the capability inventory's
    ``kind == "tool"`` rows (the loader's own verdict, covering agent/workspace
    ``capabilities/`` bundles the same way U5 resolves skills), 3) the arcagent
    builtins capabilities dir (always read-only). 404 when none locate it —
    module-derived tools (``arcagent/modules/*/capabilities.py``) and
    live-registry-only entries carry no editable file surface today.
    """
    agent_id = request.path_params["id"]
    tool_name = request.path_params["tool_name"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)

    disk_hit = _locate_disk_tool_file(agent_root, tool_name)
    if disk_hit is not None:
        file_path, transport = disk_hit
        meta = next(
            (
                row
                for row in _parse_tool_blocks(_read_text_or_empty(file_path))
                if row["name"] == tool_name
            ),
            {"classification": "", "description": ""},
        )
        payload = _tool_detail_payload(
            name=tool_name,
            transport=transport,
            classification=meta.get("classification", ""),
            description=meta.get("description", ""),
            source_path=file_path,
            editable=transport in _EDITABLE_TOOL_TRANSPORTS,
            agent_root=agent_root,
        )
        return JSONResponse(payload.model_dump(mode="json"))

    from arcui.routes.agent_detail.capabilities import _live_agent, agent_tool_rows

    # Only consult agent/workspace-sourced rows here — builtins/global route
    # through the dedicated steps below so the transport label stays the
    # canonical "builtin" the Tools tab list already uses, not the loader's
    # raw "builtins"/"builtins-skills" scan-root name.
    cap_rows = await agent_tool_rows(agent_root, _live_agent(request, agent_id))
    cap_matches = [
        r
        for r in cap_rows
        if r.get("name") == tool_name
        and r.get("source_path")
        and str(r.get("source_root") or "").startswith(("agent", "workspace"))
    ]
    if cap_matches:
        row = cap_matches[-1]  # last root wins, mirroring skills._skill_dir
        source_path = Path(str(row["source_path"]))
        payload = _tool_detail_payload(
            name=tool_name,
            transport=str(row.get("source_root") or ""),
            classification="",
            description=str(row.get("description") or ""),
            source_path=source_path,
            editable=True,
            agent_root=agent_root,
        )
        return JSONResponse(payload.model_dump(mode="json"))

    builtin_path = _locate_builtin_tool_file(tool_name)
    if builtin_path is not None:
        meta = next(
            (
                row
                for row in _parse_tool_blocks(_read_text_or_empty(builtin_path))
                if row["name"] == tool_name
            ),
            {"classification": "", "description": ""},
        )
        classification = meta.get("classification") or _BUILTIN_CLASSIFICATION.get(tool_name, "")
        payload = _tool_detail_payload(
            name=tool_name,
            transport="builtin",
            classification=classification,
            description=meta.get("description", ""),
            source_path=builtin_path,
            editable=False,
            agent_root=agent_root,
        )
        return JSONResponse(payload.model_dump(mode="json"))

    return _error(f"tool {tool_name!r} not found", 404)
