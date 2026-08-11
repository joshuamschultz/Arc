"""SPEC-064 T-020 — ``/api/keys``: the fleet's provider API keys, from the web.

``arc init`` used to end by telling the operator to append a key to ``~/.arc/.env``
by hand. This is the same store behind a browser, and it inherits the one property
that makes a web surface as safe as a terminal one (D-583): **a key value is
write-only.** :class:`~arcagent.keys.KeyStore` has no ``get``, and the response
models in :mod:`arcui.schemas` have no field able to hold a value — so there is no
route here that could return one, and no error path that could leak one either.
The refusals arcagent raises name the env var and never the rejected material.

The operator gate, the 64 KB body cap, and the error shape are
``agent_detail/config_files``'s, imported rather than restated: one recipe for
every operator-gated write in this package.
"""

from __future__ import annotations

import logging

import arcagent
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit, operator_actor_did, operator_audit_sink
from arcui.routes.agent_detail.config_files import (
    BodyTooLargeError,
    _error,
    read_json_object,
)
from arcui.schemas import (
    ProviderKeyDeleteResponse,
    ProviderKeySetResponse,
    ProviderKeysResponse,
    ProviderKeyStatus,
)

logger = logging.getLogger("arcui.routes.keys")


def _store(request: Request) -> arcagent.KeyStore:
    """The one store every surface writes provider keys to.

    ``default_env_file()`` is resolved per request rather than at import so an
    ``ARC_CONFIG_DIR`` change (a relocated deployment, a test) is honoured.
    """
    return arcagent.KeyStore(arcagent.default_env_file(), sink=operator_audit_sink(request))


async def _value_from_body(request: Request) -> str | None:
    """Read ``{"value": ...}``, or ``None`` when the body is not that shape."""
    body = await read_json_object(request)
    if body is None:
        return None
    value = body.get("value")
    return value if isinstance(value, str) else None


async def get_keys(request: Request) -> JSONResponse:
    """GET /api/keys — every provider arcllm packages, and whether a key is stored.

    Readable by ``viewer``: ``present`` is the entire answer, so this cannot tell
    a reader anything a key holder does not already know.
    """
    try:
        statuses = await _store(request).list(caller_did=operator_actor_did(request))
    except arcagent.ExtensionError as exc:
        return _error(exc.message, 400)

    return JSONResponse(
        ProviderKeysResponse(
            keys=[
                ProviderKeyStatus(
                    provider=status.provider,
                    env_var=status.env_var,
                    required=status.required,
                    present=status.present,
                )
                for status in statuses
            ]
        ).model_dump(mode="json")
    )


async def put_key(request: Request) -> JSONResponse:
    """PUT /api/keys/{env_var} — store a provider key. Operator only.

    400 when no packaged provider declares the variable (the allowlist that stops
    this from being an arbitrary-env-var write), or when the value is empty or
    carries a line break. The audit event records the coordinate; the value goes
    to the store and nowhere else.
    """
    if getattr(request.state, "role", None) != "operator":
        return _error("Operator role required", 403)

    env_var = request.path_params["env_var"]
    try:
        value = await _value_from_body(request)
    except BodyTooLargeError:
        return _error("Request body too large", 413)
    if value is None:
        return _error("Body must be a JSON object with a string 'value'", 400)

    try:
        await _store(request).set(env_var, value, caller_did=operator_actor_did(request))
    except arcagent.ExtensionError as exc:
        emit_mutation_audit(
            request,
            target=f"provider_key:{env_var}",
            operation="provider_key.write",
            outcome="denied",
            detail=exc.code,
        )
        return _error(exc.message, 400)

    emit_mutation_audit(
        request,
        target=f"provider_key:{env_var}",
        operation="provider_key.write",
        outcome="applied",
    )
    return JSONResponse(
        ProviderKeySetResponse(env_var=env_var, present=True).model_dump(mode="json")
    )


async def delete_key(request: Request) -> JSONResponse:
    """DELETE /api/keys/{env_var} — forget a provider key. Operator only.

    ``removed: false`` when there was nothing to forget: an operator cleaning up
    must not be blocked by a step with no work to do.
    """
    if getattr(request.state, "role", None) != "operator":
        return _error("Operator role required", 403)

    env_var = request.path_params["env_var"]
    try:
        removed = await _store(request).delete(env_var, caller_did=operator_actor_did(request))
    except arcagent.ExtensionError as exc:
        return _error(exc.message, 400)

    emit_mutation_audit(
        request,
        target=f"provider_key:{env_var}",
        operation="provider_key.delete",
        outcome="applied" if removed else "denied",
    )
    return JSONResponse(
        ProviderKeyDeleteResponse(env_var=env_var, present=False, removed=removed).model_dump(
            mode="json"
        )
    )


routes = [
    Route("/api/keys", get_keys, methods=["GET"]),
    Route("/api/keys/{env_var}", put_key, methods=["PUT"]),
    Route("/api/keys/{env_var}", delete_key, methods=["DELETE"]),
]

__all__ = ["delete_key", "get_keys", "put_key", "routes"]
