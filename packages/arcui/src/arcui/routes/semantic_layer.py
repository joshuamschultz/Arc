"""`/api/connections/{instance}/semantic-layer` — view/edit a datastore's meaning (H-025).

The semantic layer (``arcmemory.semantic_layer``) is consulted on every DB search
an agent runs — its table/column descriptions and sample values are
instruction-adjacent content (LLM01), so an operator's edit is a PROTECTED
artifact exactly like a prompt overlay: signed with the deployment operator key
through the same :mod:`arcui.prompt_signing` seam, a ``.arcsig`` sidecar written
beside the TOML, and verified again on every later read
(``arcmemory.semantic_layer.load_semantic_layer``). Once a layer has been
signed through this route, a direct filesystem edit desyncs its bytes from that
signature and the next read fails loud (``SemanticLayerTamperedError``) rather than
silently feeding a tampered description into an agent's context.

The read side is available to any authenticated session (viewer or operator) —
seeing what a database's tables mean is not a privileged action. The write side
mirrors ``agent_detail/prompts.py``: operator-only, secret-scanned, then signed.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from arcmemory.semantic_layer import (
    SIGNATURE_SUFFIX,
    SemanticLayer,
    SemanticLayerTamperedError,
    layer_path,
    load_semantic_layer,
)
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui import prompt_signing
from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail.files_write import _find_secret
from arcui.schemas import ErrorResponse, SemanticLayerResponse, SemanticLayerWriteResponse


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _sig_path(path: Path) -> Path:
    return path.with_name(path.name + SIGNATURE_SUFFIX)


async def get_semantic_layer(request: Request) -> JSONResponse:
    """GET .../semantic-layer — the raw TOML, its parsed classification, sign status."""
    connection_id = request.path_params["instance"]
    path = layer_path(connection_id)
    if path is None:
        return _error(f"{connection_id!r} is not a usable connection name", 400)
    if not path.is_file():
        return JSONResponse(
            SemanticLayerResponse(
                connection_id=connection_id,
                exists=False,
                content="",
                classification="unclassified",
                signed=False,
            ).model_dump(mode="json")
        )
    try:
        layer = load_semantic_layer(path)
    except SemanticLayerTamperedError as exc:
        return _error(str(exc), 409)
    content = path.read_text(encoding="utf-8")
    return JSONResponse(
        SemanticLayerResponse(
            connection_id=connection_id,
            exists=True,
            content=content,
            classification=layer.classification,
            signed=_sig_path(path).is_file(),
        ).model_dump(mode="json")
    )


async def put_semantic_layer(request: Request) -> JSONResponse:
    """PUT .../semantic-layer — author + sign an operator edit (operator only).

    Secret-scan -> parse-as-a-layer -> operator-key sign -> write -> audit,
    mirroring ``agent_detail/prompts.py._author_signed_overlay``. Once this has
    run once for a connection, every future read of the file — from this route,
    the CLI, or ``describe_datastore`` inside an agent's own turn — verifies the
    signature and fails loud on any byte that no longer matches it.
    """
    connection_id = request.path_params["instance"]
    target = f"semantic-layer:{connection_id}"
    path = layer_path(connection_id)
    if path is None:
        return _error(f"{connection_id!r} is not a usable connection name", 400)

    if getattr(request.state, "role", None) != "operator":
        emit_mutation_audit(
            request,
            target=target,
            operation="semantic_layer.write",
            outcome="denied",
            detail="viewer role",
        )
        return _error("operator_role_required", 403)

    content = await _content_from_body(request)
    if content is None:
        return _error("expected a JSON body with a string 'content' field", 400)

    secret_type = _find_secret(content)
    if secret_type is not None:
        emit_mutation_audit(
            request,
            target=target,
            operation="semantic_layer.write",
            outcome="denied",
            detail=f"secret_content:{secret_type}",
        )
        return _error(
            f"Refusing to save the semantic layer for {connection_id!r}: content looks like a "
            f"live credential ({secret_type}). Credentials never touch the filesystem.",
            400,
        )

    try:
        SemanticLayer.model_validate(tomllib.loads(content))
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="semantic_layer.write",
            outcome="denied",
            detail="unparseable",
        )
        return _error(f"content does not parse as a semantic layer: {exc}", 400)

    try:
        identity = prompt_signing.signer_for(request)
    except prompt_signing.SigningUnavailableError as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="semantic_layer.write",
            outcome="error",
            detail=str(exc),
        )
        return _error(f"cannot sign layer: {exc}", 500)

    overlay_bytes = content.encode("utf-8")
    signature = prompt_signing.sign(overlay_bytes, identity)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(overlay_bytes)
        path.chmod(0o600)
        _sig_path(path).write_text(signature.to_json(), encoding="utf-8")
    except OSError as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="semantic_layer.write",
            outcome="error",
            detail=str(exc),
        )
        return _error(f"could not write semantic layer: {exc}", 400)

    emit_mutation_audit(
        request,
        target=target,
        operation="semantic_layer.write",
        outcome="applied",
        detail=f"signer={identity.did} sha256={signature.artifact_sha256}",
    )
    return JSONResponse(
        SemanticLayerWriteResponse(
            connection_id=connection_id,
            signer_did=identity.did,
            sha256=signature.artifact_sha256,
            message="Semantic layer saved and signed. Verified on every read from now on.",
        ).model_dump(mode="json")
    )


async def _content_from_body(request: Request) -> str | None:
    try:
        body = await request.json()
    except Exception:  # reason: malformed body is a client error, not a 500
        return None
    if not isinstance(body, dict):
        return None
    content = body.get("content")
    return content if isinstance(content, str) else None


routes = [
    Route("/api/connections/{instance}/semantic-layer", get_semantic_layer, methods=["GET"]),
    Route("/api/connections/{instance}/semantic-layer", put_semantic_layer, methods=["PUT"]),
]

__all__ = ["get_semantic_layer", "put_semantic_layer", "routes"]
