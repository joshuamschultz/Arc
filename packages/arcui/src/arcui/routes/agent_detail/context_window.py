"""``GET /api/agents/{id}/context-window`` — the agent's last real chat prompt.

The Context Window card answers "how full is this agent's conversation?". That
is the size of the newest completed main-model prompt of its chat session, not
the newest model call of any kind: embeddings, memory maintenance and other
background calls are small and frequent and would hide a nearly full chat.
"""

from __future__ import annotations

from typing import Any

from arcgateway import fs_reader
from arcgateway.fs_reader import PathTraversalError
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.routes.agent_detail._common import _CALLER_DID, _agent_did, _agent_root
from arcui.routes.agent_detail.sessions import _current_session_key, _peer_map, _session_kind
from arcui.schemas import ErrorResponse

# Recent calls scanned for the newest chat prompt. Background calls outnumber chat
# prompts, so this is wide; a chat session quiet for longer than this reads as none.
_SCAN_LIMIT = 500


def _newest_chat_session(request: Request, agent_id: str, agent_did: str | None) -> str | None:
    """The newest human chat session file, for a deployment with no session router."""
    root = _agent_root(request, agent_id)
    if root is None:
        return None
    try:
        entries = fs_reader.list_tree(
            scope="agent",
            agent_id=agent_id,
            agent_root=root / "workspace",
            rel_path="sessions",
            caller_did=_CALLER_DID,
            max_depth=1,
        )
    except (PathTraversalError, FileNotFoundError):
        return None
    peers = _peer_map(request, agent_did)
    chats = [
        (float(e.mtime), e.path.rsplit("/", 1)[-1].removesuffix(".jsonl"))
        for e in entries
        if e.type == "file" and e.path.endswith(".jsonl")
    ]
    chats = [(m, sid) for m, sid in chats if _session_kind(sid, peers) == "chat"]
    return max(chats)[1] if chats else None


def _is_main_chat_prompt(trace: dict[str, Any], session_id: str) -> bool:
    """A completed inference call that a person's chat turn made in this session."""
    return (
        trace.get("status") == "success"
        and trace.get("capability_class") == "inference"
        and trace.get("job") is None
        and trace.get("session_id") == session_id
        and trace.get("call_origin") == "chat"
    )


def _prompt_summary(trace: dict[str, Any]) -> dict[str, Any]:
    return {
        "trace_id": trace["trace_id"],
        "timestamp": trace["timestamp"],
        "model": trace["model"],
        # Total prompt size: input plus cached tokens (arcllm sums them on write).
        "input_tokens": trace["input_tokens"] or 0,
        "cache_read_tokens": trace["cache_read_tokens"],
        "cache_write_tokens": trace["cache_write_tokens"],
        "output_tokens": trace["output_tokens"],
    }


async def get_context_window(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    if _agent_root(request, agent_id) is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"), status_code=404
        )
    agent_did = _agent_did(request, agent_id)
    session_id = _current_session_key(request, agent_did) or _newest_chat_session(
        request, agent_id, agent_did
    )
    prompt: dict[str, Any] | None = None
    if session_id is not None:
        traces = await request.app.state.observe.traces(
            agent=agent_did or agent_id, limit=_SCAN_LIMIT
        )
        prompt = next(
            (_prompt_summary(t) for t in traces if _is_main_chat_prompt(t, session_id)), None
        )
    router = getattr(request.app.state, "session_router", None)
    in_flight = bool(session_id and router is not None and router.is_session_in_flight(session_id))
    return JSONResponse(
        {
            "session_id": session_id,
            "turn_in_flight": in_flight,
            "prompt": prompt,
        }
    )
