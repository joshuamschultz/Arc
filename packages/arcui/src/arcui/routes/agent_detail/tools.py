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

from arcui.routes.agent_detail._common import _CALLER_DID, _agent_root
from arcui.schemas import ErrorResponse, ToolsResponse

_BUILTIN_TOOLS: tuple[tuple[str, str, str], ...] = (
    ("read", "read_only", "Read a file from disk and return its text content."),
    ("write", "state_modifying", "Write content to a file (creates or overwrites)."),
    ("edit", "state_modifying", "Edit a file in place via search/replace patch."),
    ("bash", "external_effect", "Execute a shell command in the agent workspace."),
    ("find", "read_only", "Find files by name or glob."),
    ("grep", "read_only", "Search file contents by regex."),
    ("ls", "read_only", "List directory contents."),
)
# Capture name + every keyword argument across the (...) block.
_TOOL_BLOCK_RE = re.compile(
    r"@tool\s*\(\s*(?P<body>[^@]*?)\)\s*\n\s*async\s+def|"
    r"@tool\s*\(\s*(?P<body2>[^@]*?)\)\s*\n\s*def",
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


def _arcagent_modules_dir() -> Path:
    """Locate ``arcagent/modules/`` on disk so we can scan capabilities files
    for `@tool(...)` declarations without importing the package.

    Uses ``importlib.util.find_spec`` rather than ``import arcagent`` so this
    module preserves the SPEC-023 §2.2 boundary that arcui does not import
    arcagent. The spec only locates the package's filesystem path; it does
    not execute arcagent code.
    """
    import importlib.util

    try:
        spec = importlib.util.find_spec("arcagent")
        if spec is not None and spec.origin is not None:
            return Path(spec.origin).parent / "modules"
    except (ImportError, ValueError):
        pass
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


def _collect_disk_tools(agent_root: Path) -> list[dict[str, str]]:
    """Scan agent-local + workspace tool directories for .py modules and
    parse `@tool(...)` blocks for name/classification/description metadata.

    Falls back to the file stem when there's no `@tool(...)` decorator —
    the file is still a tool surface, just one without rich metadata.
    Agent-created tools that DO use the decorator inherit the same
    classification surface as built-in/module tools.

    Locations checked (each optional):
      - team/<agent>/tools/*.py            (agent-shipped Python tools)
      - team/<agent>/workspace/tools/*.py  (agent-authored runtime tools)
      - team/<agent>/extensions/*          (extension modules)
      - team/<agent>/.capabilities/*.py    (capability dir convention)
    """
    out: list[dict[str, str]] = []
    candidates: list[tuple[Path, str]] = [
        (agent_root / "tools", "agent_dir"),
        (agent_root / "workspace" / "tools", "workspace"),
        (agent_root / "extensions", "extension"),
        (agent_root / "capabilities", "capability"),
        (agent_root / ".capabilities", "capability"),
    ]
    for path, transport in candidates:
        if not path.is_dir():
            continue
        for child in sorted(path.iterdir()):
            if child.name.startswith("_") or child.name.startswith("."):
                continue
            if child.is_file() and child.suffix == ".py":
                try:
                    text = child.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    out.append(
                        {
                            "name": child.stem,
                            "transport": transport,
                            "classification": "",
                            "description": "",
                        }
                    )
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
                    out.append(
                        {
                            "name": child.stem,
                            "transport": transport,
                            "classification": "",
                            "description": "",
                        }
                    )
            elif child.is_dir():
                # Capability folder convention — name comes from the dir.
                out.append(
                    {
                        "name": child.name,
                        "transport": transport,
                        "classification": "",
                        "description": "",
                    }
                )
    return out


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

    # Static config: [tools.policy] allowlist + denylist from arcagent.toml.
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
    for name, classification, description in _BUILTIN_TOOLS:
        _add(name, transport="builtin", classification=classification, description=description)
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
