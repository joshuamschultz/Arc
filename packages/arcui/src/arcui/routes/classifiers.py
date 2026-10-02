"""``/api/classifiers/{name}/models`` — pinned model names a classifier offers.

Feeds the Memory sharing model dropdown. The names come from the drop-in itself
(``arcllm.list_classifier_models``), so no vendor is named here. Read-only and
carries no secret, so any authenticated role may read it.
"""

from __future__ import annotations

import arcllm
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.routes.agent_detail.config_files import _error
from arcui.schemas import ClassifierModelsResponse


async def get_classifier_models(request: Request) -> JSONResponse:
    """GET /api/classifiers/{name}/models — 404 for an unknown or malformed name."""
    name = request.path_params["name"]
    try:
        models = arcllm.list_classifier_models(name)
    except (arcllm.ArcLLMClassifierUnavailableError, arcllm.ArcLLMConfigError):
        return _error("Unknown classifier", 404)
    return JSONResponse(
        ClassifierModelsResponse(classifier=name, models=list(models)).model_dump(mode="json")
    )


routes = [Route("/api/classifiers/{name}/models", get_classifier_models, methods=["GET"])]

__all__ = ["get_classifier_models", "routes"]
