# SDD — SPEC-022: ArcUI Agents + Agent Detail with Live Updates

## 1. System Overview

Two-layer architecture:

```
┌──────────────────────────────────────────────────────────────────────┐
│                       BROWSER (arcui static)                         │
│  index.html · arc-shell.js · agents-page.js · agent-detail.js · …   │
│                            │                                         │
│                  HTTP REST  │  WebSocket /ws                         │
└────────────────────────────┼─────────────────────────────────────────┘
                             │
┌────────────────────────────┼─────────────────────────────────────────┐
│                arcui (presentation, no fs access)                    │
│  routes/agent_detail.py · routes/team_pages.py · routes/ws.py       │
│  bridge.py (consumes FileChangeEvent + UIEvent)                     │
│                            │                                         │
│                            │  in-process function calls              │
└────────────────────────────┼─────────────────────────────────────────┘
                             │
┌────────────────────────────┼─────────────────────────────────────────┐
│             arcgateway (data plane, single source of truth)          │
│  fs_reader   fs_watcher   policy_parser   team_roster   config[ui]  │
│  ─────────  ──────────────────────────  ────────────────────────    │
│  stream_bridge  ─ FileChangeEvent ─►  audit  ─►  signed             │
│                            │                                         │
│                  filesystem read-only       agent ws (control)       │
└────────────────────────────┼──────────────────────────┼──────────────┘
                             ▼                          ▼
                     team/<agent>/...              agent process
                  (read-only, never written)
```

**Hard rule:** no arrow points from arcui directly to the filesystem.

## 2. Module Boundaries (Pillar 2)

| Module | Owns | Depends on |
|--------|------|------------|
| `arcgateway.fs_reader` | All read access to `team/<agent>/`. Path validation. Audit. Size caps. | `arctrust.audit`, `arcteam.Entity.workspace_path` (SPEC-019), `pathlib` |
| `arcgateway.fs_watcher` | Watcher lifecycle, ref-counting. Emits `FileChangeEvent`. | `watchfiles`, `arcgateway.stream_bridge` |
| `arcgateway.policy_parser` | Parsing ACE bullets. Pure function. | None — text in / dataclass out |
| `arcgateway.team_roster` | Discovering agents. Online overlay. | `arcteam.Entity` (SPEC-019), `arcgateway.config` |
| `arcgateway.config` (ext) | `[ui]` section parser | `tomllib` |
| `arcgateway.stream_bridge` (ext) | `FileChangeEvent` event type | (existing) |
| `arcui.routes.agent_detail` | HTTP routes for per-agent data | `arcgateway.fs_reader`, `arcgateway.policy_parser` |
| `arcui.routes.team_pages` | HTTP routes for fleet pages | `arcgateway.team_roster`, `arcgateway.fs_reader`, `arcgateway.policy_parser` |
| `arcui.routes.ws` (ext) | Subscribe/unsubscribe protocol | `arcgateway.fs_watcher` |
| `arcui.bridge` (ext) | Consume `FileChangeEvent` + UIEvent | `arcgateway.stream_bridge` |

**Forbidden imports** (CI-enforced):
- `arcui/**/*.py` may NOT import `pathlib` or `os.path` for any path under `team/`. Verified by grep guard test.
- `arcui/**/*.py` may NOT import `watchfiles`. The package is in `arcgateway` only.

## 3. Data Flow — Live Update End-to-End

```
1. agent's curator rewrites team/<agent>/workspace/policy.md
       │
2. arcgateway.fs_watcher (already running because someone subscribed)
   detects change via watchfiles.awatch()
       │
3. fs_watcher reads new content via fs_reader (audit emit: gateway.fs.read)
       │
4. fs_watcher calls policy_parser.parse(text) → list[PolicyBullet]
       │
5. fs_watcher constructs FileChangeEvent(
       agent_id, path="workspace/policy.md",
       event_type="policy:bullets_updated",
       payload={"bullets": [...]}, timestamp, sequence
   )
       │
6. fs_watcher emits via stream_bridge → audit, signing, throttling
       │
7. arcui.bridge receives FileChangeEvent (already subscribed)
       │
8. arcui.routes.ws fans event to all browser clients subscribed to this agent
       │
9. browser agent-detail.js receives WS message
       │
10. agent-detail.js re-renders Policy tab bullets card
        │
11. user sees updated bullets within 2s of disk change — no refresh
```

## 4. Component Specifications

### 4.1 `arcgateway.fs_reader`

```python
# packages/arcgateway/src/arcgateway/fs_reader.py

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from arcgateway.audit import audit_event

logger = logging.getLogger(__name__)

Scope = Literal["agent", "team", "shared"]
MAX_READ_BYTES = 1_048_576       # 1 MB
MAX_TREE_DEPTH = 10
MAX_TREE_ENTRIES = 5_000


@dataclass(frozen=True)
class FileEntry:
    path: str          # relative to scope root
    type: Literal["file", "dir"]
    size: int
    mtime: float


@dataclass(frozen=True)
class FileContent:
    path: str
    size: int
    mtime: float
    content: str       # text only — binary returns base64 with content_type="binary"
    content_type: Literal["text", "binary", "json"]


class PathTraversalError(ValueError):
    pass


class FileTooLargeError(ValueError):
    pass


def _resolve_root(scope: Scope, agent_workspace: Path | None) -> Path:
    if scope == "agent":
        if agent_workspace is None:
            raise ValueError("agent scope requires agent_workspace")
        return agent_workspace.resolve()
    if scope in ("team", "shared"):
        raise NotImplementedError(f"scope={scope!r} not implemented (forward-compat)")
    raise ValueError(f"unknown scope: {scope!r}")


def _validate_path(root: Path, rel: str) -> Path:
    candidate = (root / rel).resolve()
    try:
        common = Path(*Path(candidate).parts[: len(root.parts)])
    except Exception as e:
        raise PathTraversalError(f"invalid path: {rel}") from e
    if not str(candidate).startswith(str(root)):
        raise PathTraversalError(f"path escapes root: {rel}")
    return candidate


def read_file(
    *,
    scope: Scope,
    agent_id: str,
    agent_workspace: Path | None,
    rel_path: str,
    caller_did: str,
) -> FileContent:
    root = _resolve_root(scope, agent_workspace)
    target = _validate_path(root, rel_path)

    audit_event(
        "gateway.fs.read",
        {"scope": scope, "agent_id": agent_id, "path": rel_path, "caller_did": caller_did},
    )

    if not target.exists() or not target.is_file():
        raise FileNotFoundError(rel_path)

    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise FileTooLargeError(f"{rel_path}: {size} > {MAX_READ_BYTES}")

    # Determine content type
    if target.suffix in (".md", ".txt", ".py", ".toml", ".jsonl", ""):
        content = target.read_text(encoding="utf-8", errors="replace")
        ctype = "text"
    elif target.suffix == ".json":
        content = target.read_text(encoding="utf-8")
        ctype = "json"
    else:
        # binary fallback
        import base64
        content = base64.b64encode(target.read_bytes()).decode("ascii")
        ctype = "binary"

    return FileContent(
        path=rel_path, size=size, mtime=target.stat().st_mtime,
        content=content, content_type=ctype,
    )


def list_tree(
    *,
    scope: Scope,
    agent_id: str,
    agent_workspace: Path | None,
    rel_path: str = "",
    max_depth: int = MAX_TREE_DEPTH,
    caller_did: str,
) -> list[FileEntry]:
    root = _resolve_root(scope, agent_workspace)
    base = _validate_path(root, rel_path) if rel_path else root

    audit_event(
        "gateway.fs.tree",
        {"scope": scope, "agent_id": agent_id, "path": rel_path, "caller_did": caller_did},
    )

    entries: list[FileEntry] = []
    for child in _walk(base, max_depth):
        if len(entries) >= MAX_TREE_ENTRIES:
            break
        rel = str(child.relative_to(root))
        st = child.stat()
        entries.append(FileEntry(
            path=rel,
            type="dir" if child.is_dir() else "file",
            size=st.st_size if child.is_file() else 0,
            mtime=st.st_mtime,
        ))
    return entries


def _walk(base: Path, max_depth: int, depth: int = 0):
    if depth > max_depth:
        return
    for child in sorted(base.iterdir()):
        if child.name.startswith("."):
            continue
        yield child
        if child.is_dir():
            yield from _walk(child, max_depth, depth + 1)
```

**No write methods exist.** No `write_file`, no `mkdir`, nothing. Read-only by structure. Acceptance criterion 15 verified by integration snapshot test; criterion 17 verified by unit test that asserts `NotImplementedError` for `scope="team"` and `scope="shared"`.

### 4.2 `arcgateway.fs_watcher`

```python
# packages/arcgateway/src/arcgateway/fs_watcher.py

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcgateway.audit import audit_event
from arcgateway.fs_reader import read_file, FileNotFoundError as _FNF
from arcgateway.policy_parser import parse_bullets
from arcgateway.stream_bridge import FileChangeEvent, emit

logger = logging.getLogger(__name__)

# Watch-target → event-type map
_WATCH_MAP: dict[str, str] = {
    "arcagent.toml": "config:updated",
    "workspace/identity.md": "memory:updated",
    "workspace/policy.md": "policy:bullets_updated",
    "workspace/context.md": "memory:updated",
    "workspace/pulse.md": "pulse:updated",
    "workspace/sessions": "session:changed",   # dir-level
    "workspace/memory": "memory:updated",      # dir-level
    "workspace/notes": "memory:updated",       # dir-level
    "workspace/skills": "skills:updated",      # dir-level
    "workspace/tasks.json": "tasks:updated",
    "workspace/schedules.json": "schedules:updated",
}


@dataclass
class _WatcherEntry:
    agent_id: str
    workspace_root: Path
    refcount: int = 0
    task: asyncio.Task[None] | None = None


class WatcherManager:
    """Ref-counted per-agent watcher lifecycle. Lazy-start, lazy-teardown."""

    def __init__(self, *, max_watchers: int = 100) -> None:
        self._entries: dict[str, _WatcherEntry] = {}
        self._max_watchers = max_watchers
        self._lock = asyncio.Lock()

    async def subscribe(self, agent_id: str, workspace_root: Path) -> None:
        async with self._lock:
            entry = self._entries.get(agent_id)
            if entry is None:
                if len(self._entries) >= self._max_watchers:
                    raise RuntimeError(f"max watchers reached: {self._max_watchers}")
                entry = _WatcherEntry(agent_id=agent_id, workspace_root=workspace_root)
                self._entries[agent_id] = entry
            entry.refcount += 1
            if entry.task is None:
                entry.task = asyncio.create_task(self._run(entry))

    async def unsubscribe(self, agent_id: str) -> None:
        async with self._lock:
            entry = self._entries.get(agent_id)
            if entry is None:
                return
            entry.refcount -= 1
            if entry.refcount <= 0:
                if entry.task is not None:
                    entry.task.cancel()
                self._entries.pop(agent_id, None)

    async def _run(self, entry: _WatcherEntry) -> None:
        """Watch loop. Tries watchfiles; falls back to mtime polling."""
        try:
            from watchfiles import awatch
        except ImportError:
            await self._poll_loop(entry)
            return

        try:
            async for changes in awatch(entry.workspace_root.parent, recursive=True):
                for _change_type, path_str in changes:
                    await self._handle(entry, Path(path_str))
        except asyncio.CancelledError:
            return

    async def _poll_loop(self, entry: _WatcherEntry) -> None:
        """Stdlib fallback. 2s mtime poll."""
        seen: dict[Path, float] = {}
        try:
            while True:
                await asyncio.sleep(2.0)
                for rel, _evt in _WATCH_MAP.items():
                    target = entry.workspace_root.parent / rel
                    if not target.exists():
                        continue
                    if target.is_dir():
                        for sub in target.iterdir():
                            if sub.is_file():
                                m = sub.stat().st_mtime
                                if seen.get(sub) != m:
                                    seen[sub] = m
                                    await self._handle(entry, sub)
                    else:
                        m = target.stat().st_mtime
                        if seen.get(target) != m:
                            seen[target] = m
                            await self._handle(entry, target)
        except asyncio.CancelledError:
            return

    async def _handle(self, entry: _WatcherEntry, path: Path) -> None:
        rel = str(path.relative_to(entry.workspace_root.parent))
        event_type = _match_event_type(rel)
        if event_type is None:
            return

        payload: dict[str, Any] = {"path": rel}
        if event_type == "policy:bullets_updated":
            try:
                content = read_file(
                    scope="agent",
                    agent_id=entry.agent_id,
                    agent_workspace=entry.workspace_root,
                    rel_path="policy.md",
                    caller_did="gateway:fs_watcher",
                )
                payload["bullets"] = [b.__dict__ for b in parse_bullets(content.content)]
            except (_FNF, Exception) as e:
                logger.warning("policy parse failed for %s: %s", entry.agent_id, e)

        evt = FileChangeEvent(
            agent_id=entry.agent_id,
            event_type=event_type,
            path=rel,
            payload=payload,
        )
        await emit(evt)
        audit_event("gateway.fs.changed", {"agent_id": entry.agent_id, "path": rel, "event_type": event_type})


def _match_event_type(rel: str) -> str | None:
    if rel in _WATCH_MAP:
        return _WATCH_MAP[rel]
    for prefix, evt in _WATCH_MAP.items():
        if rel.startswith(prefix + "/"):
            return evt
    return None
```

### 4.3 `arcgateway.policy_parser`

```python
# packages/arcgateway/src/arcgateway/policy_parser.py
"""ACE policy bullet parser. Pure: text in / dataclasses out."""

import re
from dataclasses import dataclass
from datetime import date

# Format: - [P##] <text> {score:N, uses:N, reviewed:YYYY-MM-DD, created:YYYY-MM-DD, source:<sid>}
_BULLET_RE = re.compile(
    r"^\s*-\s*\[(?P<id>P\d+)\]\s*"
    r"(?P<text>.+?)\s*"
    r"\{(?P<meta>[^}]+)\}\s*$"
)
_META_RE = re.compile(r"(\w+)\s*:\s*([^,}]*)")


@dataclass(frozen=True)
class PolicyBullet:
    id: str
    text: str
    score: int
    uses: int
    reviewed: date | None
    created: date | None
    source: str
    retired: bool


def parse_bullets(text: str) -> list[PolicyBullet]:
    out: list[PolicyBullet] = []
    for line in text.splitlines():
        m = _BULLET_RE.match(line)
        if not m:
            continue
        meta = dict(_META_RE.findall(m.group("meta")))
        score = int(meta.get("score", "5"))
        out.append(PolicyBullet(
            id=m.group("id"),
            text=m.group("text").strip(),
            score=score,
            uses=int(meta.get("uses", "0")),
            reviewed=_parse_date(meta.get("reviewed", "")),
            created=_parse_date(meta.get("created", "")),
            source=meta.get("source", "").strip(),
            retired=score <= 2,
        ))
    return out


def _parse_date(s: str) -> date | None:
    s = s.strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None
```

### 4.4 `arcgateway.team_roster`

```python
# packages/arcgateway/src/arcgateway/team_roster.py
"""Discover agents on disk; overlay live status from registry."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from arcgateway.config import load_ui_section


@dataclass(frozen=True)
class RosterEntry:
    agent_id: str
    name: str
    did: str
    org: str | None
    type: str | None
    workspace_path: str
    model: str | None
    provider: str | None
    online: bool
    display_name: str
    color: str
    role_label: str
    hidden: bool


def list_team(*, team_root: Path, online_ids: set[str]) -> list[RosterEntry]:
    entries: list[RosterEntry] = []
    for agent_dir in sorted(team_root.glob("*_agent")):
        toml_path = agent_dir / "arcagent.toml"
        if not toml_path.exists():
            continue
        cfg = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        agent = cfg.get("agent", {})
        identity = cfg.get("identity", {})
        llm = cfg.get("llm", {})
        ui = load_ui_section(cfg)

        agent_id = agent.get("name") or agent_dir.name
        entries.append(RosterEntry(
            agent_id=agent_id,
            name=agent.get("name", agent_dir.name),
            did=identity.get("did", ""),
            org=agent.get("org"),
            type=agent.get("type"),
            workspace_path=str(agent_dir),
            model=llm.get("model"),
            provider=_provider_from_model(llm.get("model", "")),
            online=agent_id in online_ids,
            display_name=ui.get("display_name") or agent.get("name", agent_dir.name),
            color=ui.get("color") or _deterministic_color(agent_id),
            role_label=ui.get("role_label") or agent.get("type", ""),
            hidden=bool(ui.get("hidden", False)),
        ))
    return entries


def _provider_from_model(model: str) -> str | None:
    return model.split("/")[0] if "/" in model else None


def _deterministic_color(agent_id: str) -> str:
    import hashlib
    h = hashlib.sha256(agent_id.encode()).hexdigest()
    return f"#{h[:6]}"
```

### 4.5 `arcgateway.config` extension

```python
# Add to packages/arcgateway/src/arcgateway/config.py

def load_ui_section(toml_dict: dict) -> dict:
    """Read optional [ui] section. Empty dict if absent.

    Fields (all optional):
      display_name: str
      color: str (hex)
      role_label: str
      hidden: bool
    """
    ui = toml_dict.get("ui", {})
    if not isinstance(ui, dict):
        return {}
    return {
        "display_name": ui.get("display_name"),
        "color": ui.get("color"),
        "role_label": ui.get("role_label"),
        "hidden": ui.get("hidden", False),
    }
```

### 4.6 `arcgateway.stream_bridge` — `FileChangeEvent`

```python
# Add to packages/arcgateway/src/arcgateway/stream_bridge.py

from dataclasses import dataclass, field
from typing import Any

@dataclass
class FileChangeEvent:
    agent_id: str
    event_type: str       # "config:updated" | "policy:bullets_updated" | ...
    path: str
    payload: dict[str, Any] = field(default_factory=dict)
    # signing/timestamp/sequence injected by emit()
```

### 4.7 arcui routes

```python
# packages/arcui/src/arcui/routes/agent_detail.py
"""Per-agent HTTP routes. Thin delegators to arcgateway."""

from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcgateway import fs_reader, policy_parser, team_roster


def _workspace(request: Request, agent_id: str) -> Path | None:
    roster = request.app.state.roster_provider()
    for r in roster:
        if r.agent_id == agent_id:
            return Path(r.workspace_path) / "workspace"
    return None


async def get_config(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    ws = _workspace(request, agent_id)
    if ws is None:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    content = fs_reader.read_file(
        scope="agent", agent_id=agent_id, agent_workspace=ws,
        rel_path="../arcagent.toml",
        caller_did=request.state.caller_did,
    )
    # Parse + whitelist fields (no secrets)
    import tomllib
    cfg = tomllib.loads(content.content)
    return JSONResponse({"config": _whitelist_config(cfg), "raw": content.content})


def _whitelist_config(cfg: dict) -> dict:
    """Strip secrets. Return only display-safe fields."""
    safe = {}
    for section in ("agent", "llm", "context", "session", "telemetry", "tools.policy"):
        # walk dotted path
        cur = cfg
        for k in section.split("."):
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                cur = None; break
        if cur is not None:
            _set_dotted(safe, section, cur)
    return safe


# ... similar handlers for files/tree, files/read, skills, tools, sessions,
# stats, traces, audit, policy, policy/bullets, policy/stats, tasks, schedules.

routes = [
    Route("/api/agents/{id}/config", get_config, methods=["GET"]),
    # ... full list per PRD §F
]
```

```python
# packages/arcui/src/arcui/routes/team_pages.py
"""Fleet-level HTTP routes. Aggregations across all agents via gateway."""

from starlette.routing import Route

from arcgateway import fs_reader, policy_parser, team_roster


async def get_roster(request):
    online = {a.agent_id for a in request.app.state.agent_registry.list_agents()}
    roster = team_roster.list_team(
        team_root=request.app.state.team_root,
        online_ids=online,
    )
    return JSONResponse({"agents": [r.__dict__ for r in roster]})


# ... handlers for /api/team/policy/{bullets,stats}, /api/team/tasks,
#     /api/team/tools-skills, /api/team/audit
```

### 4.8 arcui `/ws` extension

```python
# Extend packages/arcui/src/arcui/routes/ws.py

async def _handle_client_message(ws, msg: dict, app_state):
    msg_type = msg.get("type")
    if msg_type == "subscribe:agent":
        agent_id = msg["agent_id"]
        ws_path = _workspace_for(agent_id, app_state)
        await app_state.watcher_manager.subscribe(agent_id, ws_path)
        app_state.client_subscriptions[id(ws)].add(agent_id)
    elif msg_type == "unsubscribe:agent":
        agent_id = msg["agent_id"]
        await app_state.watcher_manager.unsubscribe(agent_id)
        app_state.client_subscriptions[id(ws)].discard(agent_id)


async def _on_file_change_event(evt: FileChangeEvent, app_state):
    """Fan to all browser clients subscribed to this agent."""
    for ws_id, subs in app_state.client_subscriptions.items():
        if evt.agent_id in subs:
            await app_state.client_ws[ws_id].send_json({
                "type": "file_change",
                "agent_id": evt.agent_id,
                "event_type": evt.event_type,
                "path": evt.path,
                "payload": evt.payload,
            })
```

## 5. Frontend Architecture

### 5.1 SPA shell

`index.html` contains 8 `data-page-content` panels. `arc-shell.js` PAGES list extended:

```js
const PAGES = [
  { id: 'agents',         label: 'Agent Fleet',     icon: 'agents' },
  { id: 'agent-detail',   label: 'Agent Detail',    icon: 'agentDetail', hidden: true },
  { divider: true },
  { id: 'telemetry',      label: 'LLM Telemetry',   icon: 'telemetry' },
  { id: 'security',       label: 'Security & Audit',icon: 'security' },
  { divider: true },
  { id: 'tools-skills',   label: 'Tools & Skills',  icon: 'tools' },
  { id: 'tasks',          label: 'Tasks',           icon: 'tasks' },
  { id: 'policy',         label: 'Policy Engine',   icon: 'policy' },
  { id: 'settings',       label: 'Settings',        icon: 'settings' },
];
```

### 5.2 URL routing

```js
function readRoute() {
  const params = new URLSearchParams(location.search);
  return { page: params.get('page') || 'agents', agent: params.get('agent') };
}
function setRoute({page, agent}) {
  const p = new URLSearchParams();
  p.set('page', page);
  if (agent) p.set('agent', agent);
  history.pushState(null, '', `?${p}`);
  applyRoute();
}
window.addEventListener('popstate', applyRoute);
```

### 5.3 Tab manager (agent-detail.js)

Lazy per-tab fetcher. Each tab declares `init(agentId)` and `dispose()`. Switching tabs disposes previous, inits next. Tab data cached for 30s.

### 5.4 Live update binding

```js
const ws = new ReconnectingWebSocket('/ws');
ws.onopen = () => ws.send(JSON.stringify({type: 'subscribe:agent', agent_id: currentAgent}));
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type === 'file_change' && msg.agent_id === currentAgent) {
    dispatch(msg.event_type, msg.payload);
  }
};
window.addEventListener('beforeunload', () =>
  ws.send(JSON.stringify({type: 'unsubscribe:agent', agent_id: currentAgent}))
);
```

Each tab subscribes to relevant event types:
- Overview → `config:updated`, `pulse:updated`, `tasks:updated`, `schedules:updated`
- Sessions → `session:changed`
- Memory → `memory:updated`, `skills:updated`
- Policy → `policy:bullets_updated`
- Files → all of the above

### 5.5 Markdown renderer (`markdown.js`, ~80 LOC)

```js
function renderMarkdown(text) {
  const lines = text.split('\n');
  const out = [];
  let inFence = false, fenceLang = '', fenceBuf = [];
  let listType = null; // 'ul' | 'ol' | null
  for (const line of lines) {
    if (line.startsWith('```')) {
      if (inFence) {
        out.push(`<pre><code class="language-${fenceLang}">${escape(fenceBuf.join('\n'))}</code></pre>`);
        inFence = false; fenceBuf = []; fenceLang = '';
      } else {
        inFence = true; fenceLang = line.slice(3).trim();
      }
      continue;
    }
    if (inFence) { fenceBuf.push(line); continue; }
    if (/^#{1,6}\s/.test(line)) {
      const m = line.match(/^(#{1,6})\s+(.*)$/);
      out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`);
      continue;
    }
    if (/^\s*[-*]\s/.test(line)) {
      if (listType !== 'ul') { if (listType) out.push(`</${listType}>`); out.push('<ul>'); listType = 'ul'; }
      out.push(`<li>${inline(line.replace(/^\s*[-*]\s+/, ''))}</li>`);
      continue;
    }
    if (listType) { out.push(`</${listType}>`); listType = null; }
    if (line.startsWith('> ')) { out.push(`<blockquote>${inline(line.slice(2))}</blockquote>`); continue; }
    if (line.trim() === '') { out.push(''); continue; }
    out.push(`<p>${inline(line)}</p>`);
  }
  if (listType) out.push(`</${listType}>`);
  return out.join('\n');
}
function inline(s) {
  return escape(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');
}
function escape(s) { return s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
```

### 5.6 File tree (`file-tree.js`)

Renders tree from `GET /api/agents/{id}/files/tree`. Folder click toggles via localStorage key `arcui:tree:<agent_id>:<path>`. File click fetches via `GET /api/agents/{id}/files/read?path=...`. Markdown → `markdown.js`. Code → Prism `Prism.highlight()`.

## 6. Endpoint Surface (final)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/api/team/roster` | viewer | `arcgateway.team_roster.list_team` |
| GET | `/api/agents/{id}/config` | viewer | toml + raw, secrets stripped |
| GET | `/api/agents/{id}/files/tree?root=workspace\|agent` | viewer | depth-limited tree |
| GET | `/api/agents/{id}/files/read?path=...` | viewer | 1MB cap |
| GET | `/api/agents/{id}/skills` | viewer | parsed frontmatter from `skills/*.md` |
| GET | `/api/agents/{id}/tools` | viewer | from registration + tool source paths |
| GET | `/api/agents/{id}/sessions` | viewer | listing of `sessions/*.jsonl` |
| GET | `/api/agents/{id}/sessions/{sid}` | viewer | parsed JSONL, paginated 50 |
| GET | `/api/agents/{id}/stats?window=24h` | viewer | from `RollingAggregator` |
| GET | `/api/agents/{id}/traces?limit=50` | viewer | from `trace_store` |
| GET | `/api/agents/{id}/audit?limit=100` | viewer | from audit sink |
| GET | `/api/agents/{id}/policy` | viewer | rendered + raw + bullets |
| GET | `/api/agents/{id}/policy/bullets` | viewer | parsed bullets |
| GET | `/api/agents/{id}/policy/stats` | viewer | aggregated |
| GET | `/api/agents/{id}/tasks` | viewer | `workspace/tasks.json` |
| GET | `/api/agents/{id}/schedules` | viewer | `workspace/schedules.json` |
| GET | `/api/team/policy/bullets` | viewer | fleet aggregation |
| GET | `/api/team/policy/stats` | viewer | fleet aggregation |
| GET | `/api/team/tasks` | viewer | fleet aggregation |
| GET | `/api/team/tools-skills` | viewer | fleet aggregation |
| GET | `/api/team/audit` | viewer | fleet audit stream |
| WS | `/ws` (existing, extended) | viewer | adds `subscribe:agent` / `unsubscribe:agent` |

## 7. Test Strategy

### 7.1 arcgateway unit (TDD)
- `test_fs_reader.py` — path traversal blocked; `team`/`shared` raise NotImplementedError; size cap; binary fallback; no write methods on module API
- `test_fs_watcher.py` — lifecycle (refcount 0→1 starts, n→0 stops); polling fallback; max-watcher cap
- `test_policy_parser.py` — bullet regex (well-formed, missing fields, retired, score 0/10/-1, malformed)
- `test_team_roster.py` — synthetic team dir with mixed online/offline; `[ui]` defaults; deterministic color
- `test_ui_section_parsing.py` — fields present/absent/wrong types

### 7.2 arcui unit
- `test_agents_routes.py` (extend) — config whitelist, files tree+read, skills, tools, sessions, stats, traces, audit, policy
- `test_session_replay.py` — JSONL parsing, pagination
- `test_team_aggregations.py` — fleet roster, tasks, tools-skills, policy
- `test_ws_subscribe.py` — subscribe/unsubscribe protocol; ref-count drains gateway watcher

### 7.3 Integration
- `test_live_updates_e2e.py` — write file → assert WS event arrives within 2s; reparse policy.md works
- `test_no_team_writes.py` — snapshot SHA-256 of every file under `team/`; run all read-only operations; assert hashes unchanged
- `test_path_traversal_e2e.py` — `../../etc/passwd`, `..\..\windows`, absolute paths, symlink escape — all rejected
- `test_arcui_no_team_imports.py` — static grep over `packages/arcui/src/arcui/**/*.py`: no `pathlib.Path("team/...")` usage; no `watchfiles` import

### 7.4 Frontend (Playwright)
- Boots; sidebar matches expected pages
- Agents page: both stat boxes, grid, matrices render
- Click card → detail with `?agent=...`; all 9 tabs switchable
- Memory tree expand/collapse; markdown render; Prism highlight
- Policy: real bullets render with score-tiered colors; sort + filter work
- Pause/Restart hit control endpoint
- Live update: change `policy.md` on disk → bullets update within 2s

## 8. Quality Gates

| Gate | Threshold |
|------|-----------|
| `ruff check` | 0 errors |
| `mypy --strict` | 0 errors on new modules |
| Coverage line | ≥ 80% |
| Coverage branch | ≥ 75% |
| Coverage on new modules | ≥ 90% |
| Cold start delta | < 100ms |
| File→browser update | < 2s |

## 9. Performance Budget

| Operation | Budget |
|-----------|--------|
| Roster fetch (10 agents) | < 50ms |
| Tree listing (depth 10, 200 entries) | < 30ms |
| File read (1MB) | < 100ms |
| Policy parse (200 bullets) | < 10ms |
| WS event fan-out (100 clients) | < 20ms p95 |

## 10. Open Questions / Defer to Implementation

- **Audit sink for fleet `/api/team/audit`:** does a single sink read multi-agent audit, or do we aggregate per-agent reads? → resolve by checking existing `arcui.audit` API in implementation.
- **mTLS status on Connection Security panel:** which gateway field exposes this today? → resolve in implementation.
- **Trust Chain visual:** static render from arctrust config or live derived? → static for v1 (out of scope to add chain inspection API).
