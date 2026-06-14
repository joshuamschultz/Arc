"""`/api/agents/{id}/skills` route handler + skill-folder discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcgateway import fs_reader
from arcgateway.fs_reader import FileTooLargeError, PathTraversalError
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.routes.agent_detail._common import (
    _CALLER_DID,
    _FRONTMATTER_RE,
    _agent_root,
    logger,
)
from arcui.schemas import ErrorResponse, SkillsResponse


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
        sub = entry.path[len(rel_prefix) :] if entry.path.startswith(rel_prefix) else entry.path
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
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )
    return JSONResponse(
        SkillsResponse(skills=discover_skills(agent_id, agent_root)).model_dump(mode="json")
    )


def discover_skills(agent_id: str, agent_root: Path) -> list[dict[str, Any]]:
    """Collect an agent's skills from every standard on-disk location.

    Shared by the agent-detail Skills tab and the fleet Tools & Skills page so
    both surface the same set:
      - team/<agent>/workspace/skills/        (legacy runtime skills)
      - team/<agent>/skills/                  (legacy agent-shipped skills)
      - team/<agent>/capabilities/<name>/     (agent-shipped capabilities)
      - team/<agent>/workspace/capabilities/ (agent-authored capabilities)
      - ~/.arcagent/skills/                   (user-global, if present)
      - arcagent builtins                     (create-skill, update-tool, ...)
    """
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
    # Agent-shipped capabilities (trusted): team/<agent>/capabilities/<name>/SKILL.md.
    # Skills live directly under capabilities/, alongside @tool .py files (which
    # the .md filter skips).
    if (agent_root / "capabilities").is_dir():
        _merge(_scan_skills_dir(agent_id, agent_root / "capabilities", "", "agent_dir"))
    # Agent-authored capabilities (untrusted): workspace/capabilities/<name>/SKILL.md.
    # fs_reader skips dot-children, so the hidden dir must be the scan root itself.
    if (workspace / "capabilities").is_dir():
        _merge(_scan_skills_dir(agent_id, workspace / "capabilities", "", "workspace"))
    # Global skills dir (set by [extensions].global_dir/.. or convention)
    global_skills = Path.home() / ".arcagent" / "skills"
    if global_skills.is_dir():
        try:
            _merge(_scan_skills_dir(agent_id, global_skills.parent, "skills", "global"))
        except Exception:  # reason: fail-open — log + continue
            logger.debug("global skills scan failed", exc_info=True)
    # System-wide built-in skills shipped with arcagent (create-skill,
    # update-skill, create-tool, update-tool, ...). These are SKILL.md
    # folders — _scan_skills_dir already understands that convention.
    try:
        # importlib.util.find_spec preserves the arcui→arcagent boundary
        # (SPEC-023 §2.2) — we only need arcagent's filesystem path, never
        # its runtime behaviour.
        import importlib.util as _ilu

        spec = _ilu.find_spec("arcagent")
        if spec is not None and spec.origin is not None:
            builtin_skills = Path(spec.origin).parent / "builtins" / "capabilities" / "skills"
            if builtin_skills.is_dir():
                _merge(_scan_skills_dir(agent_id, builtin_skills.parent, "skills", "builtin"))
    except Exception:  # reason: fail-open — log + continue
        logger.debug("builtin skills scan failed", exc_info=True)

    return skills


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
