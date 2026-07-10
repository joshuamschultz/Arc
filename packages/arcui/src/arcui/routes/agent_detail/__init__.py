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

Subpackage layout
-----------------
- ``_common``   — workspace resolution helpers and shared regex/constants
  (``_agent_root``, ``_resolve_root_path``, ``_CALLER_DID``,
   ``_CONFIG_WHITELIST``, ``_VALID_SID``, ``_VALID_ROOTS``,
   ``_FRONTMATTER_RE``, ``logger``).
- ``config``    — ``get_config`` (+ whitelist) and the file-tree /
  file-read endpoints (``get_files_tree``, ``get_file_read``).
- ``skills``    — ``get_skills`` + scan/parse helpers.
- ``tools``     — ``get_tools`` + module/disk tool scanners and the
  ``@tool(...)`` decorator regex.
- ``sessions``  — ``get_sessions``, ``get_session_replay``,
  ``get_tasks``, ``get_schedules`` + JSONL/pagination helpers.
- ``telemetry`` — ``get_stats``, ``get_traces``, ``get_audit``.
- ``policy``    — ``get_policy``, ``get_policy_bullets``,
  ``get_policy_stats`` + parse helpers.

Public surface preserved: ``from arcui.routes.agent_detail import routes``
keeps working unchanged.
"""

from __future__ import annotations

from starlette.routing import Route

# Re-export shared helper so ``arcui.routes.ws`` can mirror it (it documents
# this dependency in a code comment); other internal callers continue to
# import from ``arcui.routes.agent_detail`` directly.
from arcui.routes.agent_detail._common import _agent_root  # noqa: F401
from arcui.routes.agent_detail.config import get_config, get_file_read, get_files_tree
from arcui.routes.agent_detail.files_write import put_file_write
from arcui.routes.agent_detail.policy import (
    get_policy,
    get_policy_bullets,
    get_policy_stats,
)
from arcui.routes.agent_detail.sessions import (
    get_schedules,
    get_session_replay,
    get_sessions,
    get_tasks,
)
from arcui.routes.agent_detail.skills import get_skills
from arcui.routes.agent_detail.telemetry import get_audit, get_stats, get_traces
from arcui.routes.agent_detail.tools import get_tools

routes = [
    Route("/api/agents/{id}/config", get_config, methods=["GET"]),
    Route("/api/agents/{id}/files/tree", get_files_tree, methods=["GET"]),
    Route("/api/agents/{id}/files/read", get_file_read, methods=["GET"]),
    Route("/api/agents/{id}/files/read", put_file_write, methods=["PUT"]),
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

__all__ = ["routes"]
