"""``/api/classifiers/{name}/models`` — pinned model names a classifier offers.

Feeds the Memory sharing model dropdown. The names come from the drop-in itself
(``arcagent.classifier_models``), so no vendor is named here. Read-only and
carries no secret, so any authenticated role may read it.
"""

from __future__ import annotations

import arcagent
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.routes.agent_detail.config_files import _error
from arcui.schemas import ClassifierModelsResponse


async def get_classifier_models(request: Request) -> JSONResponse:
    """GET /api/classifiers/{name}/models — 404 for an unknown or malformed name."""
    name = request.path_params["name"]
    models = arcagent.classifier_models(name)
    if models is None:
        return _error("Unknown classifier", 404)
    return JSONResponse(
        ClassifierModelsResponse(classifier=name, models=list(models)).model_dump(mode="json")
    )


routes = [Route("/api/classifiers/{name}/models", get_classifier_models, methods=["GET"])]

__all__ = ["get_classifier_models", "routes"]
