"""The one signed-write envelope: policy gate -> operator-key sign -> atomic write -> audit.

Every operator edit that becomes signed control-plane text goes through
:func:`sign_and_write` — a catalog prompt override (``prompts.py``) and a signed
workspace document such as ``identity.md`` (``files_write.py``) alike — so the
signer is always the RESOLVED operator principal (``arcui.prompt_signing``), the
optional ``prompt:write`` policy gate always runs, and the audited signer DID is
the one that signed.

The text and its detached ``.arcsig`` sidecar are two files, so they are written
as a pair: both land in temp files first, then are renamed into place, and a
failure between the two renames puts the previous text back. A reader therefore
never meets new text beside an old signature — which the agent would reject,
refusing every run (J2 F7).

Every successful save is also appended to the agent's :class:`arcprompt.PromptHistory`
(signed, immutable prior versions — J2 F3), so "go back to Tuesday's" is possible. A
signed text that was already live before history existed is captured first, so the
operator's last pre-history edit survives their next save.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from arcprompt import PromptHistory, record_if_unseen
from arctrust.policy import Decision, PolicyContext, ToolCall, read_agent_tier
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui import prompt_signing
from arcui.audit import emit_mutation_audit
from arcui.schemas import ErrorResponse

_TMP_SUFFIX = ".tmp"


@dataclass(frozen=True)
class SignedWrite:
    """The outcome of a signed write: who signed, the digest, and the history version."""

    signer_did: str
    sha256: str
    version: int


def error_response(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


async def evaluate_policy(
    request: Request, *, agent_root: Path, agent_did: str, package: str, name: str
) -> Decision | None:
    """Route a ``prompt:write`` action through the optional injected policy pipeline.

    Returns the pipeline's :class:`Decision`, or ``None`` when no pipeline is
    configured (configured-gate convention: no rule -> allow). arcui holds no
    tier-conditional logic — the pipeline it is handed decides.
    """
    pipeline = getattr(request.app.state, "prompt_policy", None)
    if pipeline is None:
        return None
    session_id = getattr(request.state, "session_id", None) or "unknown"
    call = ToolCall(
        tool_name="prompt:write",
        arguments={"package": package, "name": name},
        agent_did=agent_did,
        session_id=session_id,
        classification="unclassified",
    )
    ctx = PolicyContext(
        tier=read_agent_tier(agent_root),
        policy_version="",
        bundle_age_seconds=0.0,
    )
    decision: Decision = await pipeline.evaluate(call, ctx)
    return decision


def _write_durable(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` and flush it to disk before returning."""
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def write_pair_atomically(path: Path, data: bytes, sidecar: Path, sidecar_text: str) -> None:
    """Replace ``path`` and ``sidecar`` together: temp files, then rename, rollback on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    previous = path.read_bytes() if path.is_file() else None
    tmp_data = path.with_name(path.name + _TMP_SUFFIX)
    tmp_sidecar = sidecar.with_name(sidecar.name + _TMP_SUFFIX)
    try:
        _write_durable(tmp_data, data)
        _write_durable(tmp_sidecar, sidecar_text.encode("utf-8"))
        os.replace(tmp_data, path)
        try:
            os.replace(tmp_sidecar, sidecar)
        except OSError:
            _restore(path, previous)
            raise
    finally:
        tmp_data.unlink(missing_ok=True)
        tmp_sidecar.unlink(missing_ok=True)


def _capture_live_version(history: PromptHistory, path: Path, sidecar: Path) -> None:
    """Record the signed text currently live (if any) before it is replaced."""
    if path.is_file() and sidecar.is_file():
        try:
            record_if_unseen(history, path.read_bytes(), sidecar.read_text(encoding="utf-8"))
        except ValueError:  # reason: an unparseable live sidecar is not a signed version
            return


def _restore(path: Path, previous: bytes | None) -> None:
    """Put back the text that was there before a failed pair write (best effort)."""
    if previous is None:
        path.unlink(missing_ok=True)
        return
    restore_tmp = path.with_name(path.name + _TMP_SUFFIX)
    _write_durable(restore_tmp, previous)
    os.replace(restore_tmp, path)


async def sign_and_write(
    request: Request,
    *,
    agent_root: Path,
    agent_did: str,
    package: str,
    name: str,
    target: str,
    operation: str,
    path: Path,
    data: bytes,
    sidecar: Path,
    audit_note: str = "",
) -> SignedWrite | JSONResponse:
    """Gate, sign and write ``data`` to ``path`` with its sidecar; audit the outcome.

    Returns the :class:`SignedWrite` on success, or the error response the route
    should return (policy denied, no signing key, unwritable). Nothing is written
    unless the signature was produced first.
    """
    decision = await evaluate_policy(
        request, agent_root=agent_root, agent_did=agent_did, package=package, name=name
    )
    if decision is not None and decision.is_deny():
        detail = f"policy_denied:{decision.layer}:{decision.rule_id}"
        emit_mutation_audit(
            request, target=target, operation=operation, outcome="denied", detail=detail
        )
        return error_response(f"prompt override denied by policy: {decision.reason}", 403)

    try:
        identity = prompt_signing.signer_for(request)
    except prompt_signing.SigningUnavailableError as exc:
        emit_mutation_audit(
            request, target=target, operation=operation, outcome="error", detail=str(exc)
        )
        return error_response(f"cannot sign override: {exc}", 500)

    signature = prompt_signing.sign(data, identity)
    history = PromptHistory(agent_root, package, name)
    try:
        _capture_live_version(history, path, sidecar)
        write_pair_atomically(path, data, sidecar, signature.to_json())
        recorded = history.record(data, signature.to_json())
    except OSError as exc:
        emit_mutation_audit(
            request, target=target, operation=operation, outcome="error", detail=str(exc)
        )
        return error_response(f"could not write override: {exc}", 400)

    emit_mutation_audit(
        request,
        target=target,
        operation=operation,
        outcome="applied",
        detail=f"signer={identity.did} sha256={signature.artifact_sha256}{audit_note}",
    )
    return SignedWrite(
        signer_did=identity.did, sha256=signature.artifact_sha256, version=recorded.version
    )


__all__ = [
    "SignedWrite",
    "error_response",
    "evaluate_policy",
    "sign_and_write",
    "write_pair_atomically",
]
