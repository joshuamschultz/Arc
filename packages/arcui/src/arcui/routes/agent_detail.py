"""Per-agent HTTP routes for the Agent Detail screen.

Each handler is a thin delegator into ``arcgateway.fs_reader`` /
``arcgateway.policy_parser``. The routes own:

* path-param → workspace lookup via ``request.app.state.roster_provider()``
  (no direct ``team/`` filesystem access from arcui — SPEC-022 acceptance #16),
* HTTP error mapping (404 unknown agent, 400 traversal, 404 missing file),
* config field whitelisting (no secrets ever leave the gateway),
* JSONL session pagination,
* in-memory audit-buffer projection scoped to the agent.

All file content reaches the browser through ``fs_reader``'s single audited
chokepoint — there is no other path. Endpoint surface mirrors SDD §6.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from collections import deque
from pathlib import Path
from typing import Any

from arcgateway import fs_reader, policy_parser
from arcgateway.fs_reader import (
    FileTooLargeError,
    PathTraversalError,
)
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

# Whitelisted top-level config sections — anything else (e.g. ``[secrets]``,
# ``[identity.private_key]``) is dropped before serialization. Keep this list
# tight; it is the security boundary for LLM07 (system prompt leakage).
_CONFIG_WHITELIST: tuple[str, ...] = (
    "agent",
    "llm",
    "context",
    "session",
    "telemetry",
    "tools",
    # SPEC-022 Policy tab needs eval_interval_turns / max_bullets / etc. These
    # sections never carry secrets — `modules.<name>.config` is wiring data,
    # `eval` is the reflection model config (model name, fallback, timeout).
    "modules",
    "eval",
    "extensions",
    "team",
    "vault",
    "identity",
)

# Caller DID used in audit events. arcui has no per-user DID today; this is
# the gateway-side actor for "ui requested this read."
_CALLER_DID = "did:arc:ui:viewer"

# Session id format: alphanumeric / dash / underscore / dot only. Defends
# against ``../`` injection on session replay (path component, not query).
_VALID_SID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Roots accepted by /files/* endpoints. ``workspace`` resolves to
# ``team/<agent>/workspace/``; ``agent`` to ``team/<agent>/``.
_VALID_ROOTS = frozenset({"workspace", "agent"})

# Frontmatter delimiter for skill files — three dashes on a line by themselves.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------


def _agent_root(request: Request, agent_id: str) -> Path | None:
    """Look up an agent's filesystem root via the injected roster provider.

    Returns the absolute path to ``team/<dir>_agent/`` (the agent root, NOT
    the workspace subdir). Callers select the workspace root explicitly via
    the ``root`` query param.
    """
    provider = getattr(request.app.state, "roster_provider", None)
    if provider is None:
        return None
    for entry in provider():
        if entry.agent_id == agent_id:
            return Path(entry.workspace_path)
    return None


def _resolve_root_path(agent_root: Path, root_arg: str) -> Path:
    """Map ``root_arg`` to the resolved filesystem path."""
    if root_arg == "workspace":
        return agent_root / "workspace"
    return agent_root


# ---------------------------------------------------------------------------
# /api/agents/{id}/config
# ---------------------------------------------------------------------------


async def get_config(request: Request) -> JSONResponse:
    """Return the agent's whitelisted config + raw TOML.

    The whitelisted ``config`` object is the safe surface — drop any section
    not on :data:`_CONFIG_WHITELIST`. Raw text is returned as a separate
    ``raw`` field for the operator's "View raw" toggle, which already lives
    on the gateway side of the trust boundary (the operator can read these
    files directly, this is just convenience).
    """
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    try:
        content = fs_reader.read_file(
            scope="agent",
            agent_id=agent_id,
            agent_root=agent_root,
            rel_path="arcagent.toml",
            caller_did=_CALLER_DID,
        )
    except FileNotFoundError:
        return JSONResponse({"error": "arcagent.toml not found"}, status_code=404)
    except (PathTraversalError, FileTooLargeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    try:
        parsed = tomllib.loads(content.content)
    except tomllib.TOMLDecodeError as exc:
        return JSONResponse({"error": f"invalid toml: {exc}"}, status_code=500)

    return JSONResponse(
        {
            "config": _whitelist_config(parsed),
            "raw": content.content,
            "mtime": content.mtime,
        }
    )


def _whitelist_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Keep only top-level sections on the whitelist; drop everything else."""
    return {k: cfg[k] for k in _CONFIG_WHITELIST if k in cfg}


# ---------------------------------------------------------------------------
# /api/agents/{id}/files/tree, /api/agents/{id}/files/read
# ---------------------------------------------------------------------------


async def get_files_tree(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    root_arg = request.query_params.get("root", "workspace")
    if root_arg not in _VALID_ROOTS:
        return JSONResponse({"error": "Invalid root"}, status_code=400)

    base = _resolve_root_path(agent_root, root_arg)
    try:
        entries = fs_reader.list_tree(
            scope="agent",
            agent_id=agent_id,
            agent_root=base,
            rel_path="",
            caller_did=_CALLER_DID,
        )
    except PathTraversalError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse(
        {
            "root": root_arg,
            "entries": [
                {"path": e.path, "type": e.type, "size": e.size, "mtime": e.mtime}
                for e in entries
            ],
        }
    )


async def get_file_read(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    rel = request.query_params.get("path")
    if not rel:
        return JSONResponse({"error": "Missing path"}, status_code=400)

    root_arg = request.query_params.get("root", "workspace")
    if root_arg not in _VALID_ROOTS:
        return JSONResponse({"error": "Invalid root"}, status_code=400)

    base = _resolve_root_path(agent_root, root_arg)

    try:
        content = fs_reader.read_file(
            scope="agent",
            agent_id=agent_id,
            agent_root=base,
            rel_path=rel,
            caller_did=_CALLER_DID,
        )
    except FileNotFoundError:
        return JSONResponse({"error": "File not found"}, status_code=404)
    except PathTraversalError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileTooLargeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=413)

    return JSONResponse(
        {
            "path": content.path,
            "size": content.size,
            "mtime": content.mtime,
            "content": content.content,
            "content_type": content.content_type,
        }
    )


# ---------------------------------------------------------------------------
# /api/agents/{id}/skills
# ---------------------------------------------------------------------------


def _scan_skills_dir(
    agent_id: str, scope_root: Path, rel_dir: str, source: str
) -> list[dict[str, Any]]:
    """Walk one skills directory through fs_reader (audited + sandboxed).

    `source` tags each row so the UI can show where a skill came from
    (workspace / agent-dir / module). Folders containing SKILL.md are
    treated as compound skills; loose .md files are treated as flat.
    """
    out: list[dict[str, Any]] = []
    try:
        entries = fs_reader.list_tree(
            scope="agent",
            agent_id=agent_id,
            agent_root=scope_root,
            rel_path=rel_dir,
            caller_did=_CALLER_DID,
            max_depth=2,
        )
    except (PathTraversalError, FileNotFoundError):
        return out

    # Two valid skill shapes:
    #   1. Compound skill: skills/<name>/SKILL.md (one level deep)
    #   2. Flat skill:     skills/<name>.md       (immediate child)
    # Everything else under a skill folder (references/*.md, examples/*.md,
    # nested helpers) is supporting material and must NOT be surfaced as a
    # top-level skill. Filter strictly by relative depth.
    rel_prefix = (rel_dir + "/") if rel_dir else ""
    for entry in entries:
        if entry.type == "dir":
            continue
        if not entry.path.endswith(".md"):
            continue
        # entry.path is relative to scope_root; subtract the rel_dir prefix
        # so we can reason about depth from the skills/ root.
        sub = entry.path[len(rel_prefix):] if entry.path.startswith(rel_prefix) else entry.path
        depth = sub.count("/")
        is_skill_md = sub.endswith("/SKILL.md") and depth == 1
        is_flat_md = depth == 0
        if not (is_skill_md or is_flat_md):
            continue
        try:
            content = fs_reader.read_file(
                scope="agent",
                agent_id=agent_id,
                agent_root=scope_root,
                rel_path=entry.path,
                caller_did=_CALLER_DID,
            )
        except (FileNotFoundError, PathTraversalError, FileTooLargeError):
            continue
        skill = _parse_skill(entry.path, content.content)
        skill["mtime"] = entry.mtime
        skill["source"] = source
        # Inline the body so the UI doesn't have to make a second
        # /files/read call (which only resolves workspace paths and
        # would 404 for builtin/global skills that live outside).
        skill["body"] = content.content
        out.append(skill)
    return out


async def get_skills(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    # Pull skills from every standard location the agent could have them:
    #   - team/<agent>/workspace/skills/      (agent-authored runtime skills)
    #   - team/<agent>/skills/                (agent-shipped skills)
    #   - ~/.arcagent/skills/                 (user-global skills shared
    #                                          across agents, if directory
    #                                          exists)
    skills: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _merge(rows: list[dict[str, Any]]) -> None:
        for s in rows:
            key = s.get("name") or s.get("path")
            if key and key not in seen:
                seen.add(key)
                skills.append(s)

    workspace = agent_root / "workspace"
    if workspace.is_dir():
        _merge(_scan_skills_dir(agent_id, workspace, "skills", "workspace"))
    if (agent_root / "skills").is_dir():
        _merge(_scan_skills_dir(agent_id, agent_root, "skills", "agent_dir"))
    # Global skills dir (set by [extensions].global_dir/.. or convention)
    global_skills = Path.home() / ".arcagent" / "skills"
    if global_skills.is_dir():
        try:
            _merge(_scan_skills_dir(agent_id, global_skills.parent, "skills", "global"))
        except Exception:
            pass
    # System-wide built-in skills shipped with arcagent (create-skill,
    # update-skill, create-tool, update-tool, ...). These are SKILL.md
    # folders — _scan_skills_dir already understands that convention.
    try:
        import arcagent
        builtin_skills = Path(arcagent.__file__).parent / "builtins" / "capabilities" / "skills"
        if builtin_skills.is_dir():
            _merge(_scan_skills_dir(
                agent_id, builtin_skills.parent, "skills", "builtin"
            ))
    except Exception:
        pass

    return JSONResponse({"skills": skills})


def _parse_skill(rel_path: str, text: str) -> dict[str, Any]:
    """Parse YAML-ish frontmatter (``key: value`` lines) into a dict.

    Skill markdown frontmatter is intentionally simple — flat ``key: value``
    pairs only, no nested mappings. We keep the parser equally simple to
    avoid pulling in PyYAML for one feature.
    """
    name = rel_path.removesuffix(".md").rsplit("/", 1)[-1]
    fm: dict[str, Any] = {}
    match = _FRONTMATTER_RE.match(text)
    if match:
        for line in match.group(1).splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                fm[key.strip()] = value.strip()
    return {
        "name": fm.get("name", name),
        "description": fm.get("description", ""),
        "version": fm.get("version", ""),
        "path": f"skills/{rel_path.split('skills/', 1)[-1]}"
        if "skills/" in rel_path
        else f"skills/{rel_path}",
    }


# ---------------------------------------------------------------------------
# /api/agents/{id}/tools
# ---------------------------------------------------------------------------


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
    r'@tool\s*\(\s*(?P<body>[^@]*?)\)\s*\n\s*async\s+def|'
    r'@tool\s*\(\s*(?P<body2>[^@]*?)\)\s*\n\s*def',
    re.DOTALL,
)
_KW_NAME_RE = re.compile(r'name\s*=\s*["\']([^"\']+)["\']')
_KW_CLASS_RE = re.compile(r'classification\s*=\s*["\']([^"\']+)["\']')
_KW_DESC_RE = re.compile(r'description\s*=\s*(?P<q>["\']{1,3})(?P<text>.+?)(?P=q)', re.DOTALL)


def _arcagent_modules_dir() -> Path:
    """Locate ``arcagent/modules/`` on disk so we can scan capabilities files
    for `@tool(...)` declarations without importing the package."""
    try:
        import arcagent
        return Path(arcagent.__file__).parent / "modules"
    except Exception:
        return Path(__file__).resolve().parents[5] / "arcagent/src/arcagent/modules"


def _parse_tool_blocks(text: str) -> list[dict[str, str]]:
    """Pull (name, classification, description) from every @tool(...) in a
    capabilities.py source. Robust to single/double/triple quotes and
    line wraps inside the description. Missing fields surface as ''."""
    rows: list[dict[str, str]] = []
    for m in _TOOL_BLOCK_RE.finditer(text):
        body = m.group("body") or m.group("body2") or ""
        name_m = _KW_NAME_RE.search(body)
        if not name_m:
            continue
        cls_m = _KW_CLASS_RE.search(body)
        desc_m = _KW_DESC_RE.search(body)
        rows.append({
            "name": name_m.group(1),
            "classification": cls_m.group(1) if cls_m else "",
            "description": desc_m.group("text").strip() if desc_m else "",
        })
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
            out.append({
                "name": row["name"],
                "transport": f"module:{mod_name}",
                "classification": row["classification"],
                "description": row["description"],
                "status": "allow" if enabled else "inactive",
            })
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
                    out.append({"name": child.stem, "transport": transport,
                                "classification": "", "description": ""})
                    continue
                blocks = _parse_tool_blocks(text)
                if blocks:
                    for row in blocks:
                        out.append({
                            "name": row["name"],
                            "transport": transport,
                            "classification": row.get("classification") or "",
                            "description": row.get("description") or "",
                        })
                else:
                    out.append({"name": child.stem, "transport": transport,
                                "classification": "", "description": ""})
            elif child.is_dir():
                # Capability folder convention — name comes from the dir.
                out.append({"name": child.name, "transport": transport,
                            "classification": "", "description": ""})
    return out


async def get_tools(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

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
        policy = cfg.get("tools", {}).get("policy", {}) if isinstance(
            cfg.get("tools"), dict
        ) else {}
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
        row = {"name": name, "transport": "", "classification": "",
               "description": "", "status": "allow"}
        row.update({k: v for k, v in fields.items() if v not in (None, "")})
        if name in denylist:
            row["status"] = "deny"
        seen[name] = row

    for t in live_tools:
        _add(t, transport="registered")
    for name, classification, description in _BUILTIN_TOOLS:
        _add(name, transport="builtin", classification=classification,
             description=description)
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
        {
            "tools": tools,
            "allowlist": allowlist,
            "denylist": denylist,
        }
    )


# ---------------------------------------------------------------------------
# /api/agents/{id}/sessions, /api/agents/{id}/sessions/{sid}
# ---------------------------------------------------------------------------


async def get_sessions(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    workspace = agent_root / "workspace"
    try:
        entries = fs_reader.list_tree(
            scope="agent",
            agent_id=agent_id,
            agent_root=workspace,
            rel_path="sessions",
            caller_did=_CALLER_DID,
            max_depth=1,
        )
    except PathTraversalError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    sessions: list[dict[str, Any]] = []
    for entry in entries:
        if entry.type != "file" or not entry.path.endswith(".jsonl"):
            continue
        sid = entry.path.rsplit("/", 1)[-1].removesuffix(".jsonl")
        sessions.append(
            {
                "sid": sid,
                "path": entry.path,
                "size": entry.size,
                "mtime": entry.mtime,
            }
        )
    sessions.sort(key=lambda s: float(s["mtime"]), reverse=True)
    return JSONResponse({"sessions": sessions})


async def get_session_replay(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    sid = request.path_params["sid"]
    if not _VALID_SID.match(sid):
        return JSONResponse({"error": "Invalid session id"}, status_code=400)

    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    page, page_size, err = _parse_pagination(request)
    if err is not None:
        return err

    workspace = agent_root / "workspace"
    try:
        content = fs_reader.read_file(
            scope="agent",
            agent_id=agent_id,
            agent_root=workspace,
            rel_path=f"sessions/{sid}.jsonl",
            caller_did=_CALLER_DID,
        )
    except FileNotFoundError:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    except (PathTraversalError, FileTooLargeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    messages = _parse_jsonl(content.content)
    total = len(messages)
    start = (page - 1) * page_size
    end = start + page_size
    return JSONResponse(
        {
            "sid": sid,
            "page": page,
            "page_size": page_size,
            "total": total,
            "messages": messages[start:end],
        }
    )


def _parse_pagination(request: Request) -> tuple[int, int, JSONResponse | None]:
    raw_page = request.query_params.get("page", "1")
    raw_size = request.query_params.get("page_size", "50")
    try:
        page = max(1, int(raw_page))
        page_size = max(1, min(200, int(raw_size)))
    except ValueError:
        return 0, 0, JSONResponse({"error": "Invalid pagination"}, status_code=400)
    return page, page_size, None


def _parse_jsonl(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# ---------------------------------------------------------------------------
# /api/agents/{id}/stats, /api/agents/{id}/traces
# ---------------------------------------------------------------------------


async def get_stats(request: Request) -> JSONResponse:
    """Per-agent stats — delegates to per-agent or global aggregator.

    Mirrors the behaviour of the existing ``/api/stats?agent_id=`` route but
    uses the path-param style for symmetry with the agent-detail screen.
    """
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    registry = request.app.state.agent_registry
    entry = registry.get(agent_id)
    aggregator = entry.aggregator if entry and entry.aggregator else getattr(
        request.app.state, "aggregator", None
    )
    if aggregator is None:
        return JSONResponse({"stats": {}, "window": "24h"})
    window = request.query_params.get("window", "24h")
    if window not in {"1h", "24h", "7d"}:
        return JSONResponse({"error": "Invalid window"}, status_code=400)
    return JSONResponse({"stats": aggregator.stats(window), "window": window})


async def get_traces(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    store = request.app.state.trace_store
    if store is None:
        return JSONResponse({"traces": [], "cursor": None})

    try:
        limit = max(1, min(500, int(request.query_params.get("limit", "50"))))
    except ValueError:
        return JSONResponse({"error": "Invalid limit"}, status_code=400)

    records, cursor = await store.query(limit=limit, agent=agent_id)
    return JSONResponse(
        {
            "traces": [r.model_dump() for r in records],
            "cursor": cursor,
        }
    )


# ---------------------------------------------------------------------------
# /api/agents/{id}/audit
# ---------------------------------------------------------------------------


async def get_audit(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    buffer: deque[dict[str, Any]] = getattr(request.app.state, "audit_buffer", None) or deque()
    try:
        limit = max(1, min(1000, int(request.query_params.get("limit", "100"))))
    except ValueError:
        return JSONResponse({"error": "Invalid limit"}, status_code=400)

    events = [e for e in buffer if e.get("agent_id") == agent_id][-limit:]
    return JSONResponse({"events": events})


# ---------------------------------------------------------------------------
# /api/agents/{id}/policy, /policy/bullets, /policy/stats
# ---------------------------------------------------------------------------


async def get_policy(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    workspace = agent_root / "workspace"
    raw, err = _read_policy(agent_id, workspace)
    if err is not None:
        return err
    bullets = [_bullet_to_dict(b) for b in policy_parser.parse_bullets(raw)]
    return JSONResponse({"raw": raw, "bullets": bullets})


async def get_policy_bullets(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    workspace = agent_root / "workspace"
    raw, err = _read_policy(agent_id, workspace)
    if err is not None:
        return err
    bullets = [_bullet_to_dict(b) for b in policy_parser.parse_bullets(raw)]
    return JSONResponse({"bullets": bullets})


async def get_policy_stats(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    workspace = agent_root / "workspace"
    raw, err = _read_policy(agent_id, workspace)
    if err is not None:
        return err
    bullets = policy_parser.parse_bullets(raw)
    active = [b for b in bullets if not b.retired]
    retired = [b for b in bullets if b.retired]
    avg = sum(b.score for b in active) / len(active) if active else 0.0
    return JSONResponse(
        {
            "total": len(bullets),
            "active": len(active),
            "retired": len(retired),
            "avg_score": avg,
        }
    )


def _read_policy(agent_id: str, workspace: Path) -> tuple[str, JSONResponse | None]:
    try:
        content = fs_reader.read_file(
            scope="agent",
            agent_id=agent_id,
            agent_root=workspace,
            rel_path="policy.md",
            caller_did=_CALLER_DID,
        )
    except FileNotFoundError:
        return "", None  # missing policy is treated as empty, not an error
    except (PathTraversalError, FileTooLargeError) as exc:
        return "", JSONResponse({"error": str(exc)}, status_code=400)
    return content.content, None


def _bullet_to_dict(b: policy_parser.PolicyBullet) -> dict[str, Any]:
    return {
        "id": b.id,
        "text": b.text,
        "score": b.score,
        "uses": b.uses,
        "reviewed": b.reviewed.isoformat() if b.reviewed else None,
        "created": b.created.isoformat() if b.created else None,
        "source": b.source,
        "retired": b.retired,
    }


# ---------------------------------------------------------------------------
# /api/agents/{id}/tasks, /api/agents/{id}/schedules
# ---------------------------------------------------------------------------


async def get_tasks(request: Request) -> JSONResponse:
    return await _read_json_array(request, rel_path="tasks.json", key="tasks")


async def get_schedules(request: Request) -> JSONResponse:
    return await _read_json_array(request, rel_path="schedules.json", key="schedules")


async def _read_json_array(
    request: Request, *, rel_path: str, key: str
) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    workspace = agent_root / "workspace"
    try:
        content = fs_reader.read_file(
            scope="agent",
            agent_id=agent_id,
            agent_root=workspace,
            rel_path=rel_path,
            caller_did=_CALLER_DID,
        )
    except FileNotFoundError:
        return JSONResponse({key: []})
    except (PathTraversalError, FileTooLargeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    try:
        parsed = json.loads(content.content)
    except json.JSONDecodeError:
        return JSONResponse({key: []})
    if not isinstance(parsed, list):
        return JSONResponse({key: []})
    return JSONResponse({key: parsed})


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------


routes = [
    Route("/api/agents/{id}/config", get_config, methods=["GET"]),
    Route("/api/agents/{id}/files/tree", get_files_tree, methods=["GET"]),
    Route("/api/agents/{id}/files/read", get_file_read, methods=["GET"]),
    Route("/api/agents/{id}/skills", get_skills, methods=["GET"]),
    Route("/api/agents/{id}/tools", get_tools, methods=["GET"]),
    Route("/api/agents/{id}/sessions", get_sessions, methods=["GET"]),
    Route("/api/agents/{id}/sessions/{sid}", get_session_replay, methods=["GET"]),
    Route("/api/agents/{id}/stats", get_stats, methods=["GET"]),
    Route("/api/agents/{id}/traces", get_traces, methods=["GET"]),
    Route("/api/agents/{id}/audit", get_audit, methods=["GET"]),
    Route("/api/agents/{id}/policy", get_policy, methods=["GET"]),
    Route("/api/agents/{id}/policy/bullets", get_policy_bullets, methods=["GET"]),
    Route("/api/agents/{id}/policy/stats", get_policy_stats, methods=["GET"]),
    Route("/api/agents/{id}/tasks", get_tasks, methods=["GET"]),
    Route("/api/agents/{id}/schedules", get_schedules, methods=["GET"]),
]
