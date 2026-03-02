"""Export route — /api/export (CSV/JSON)."""

from __future__ import annotations

import csv
import io
import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

_MAX_EXPORT_LIMIT = 10000


async def export_traces(request: Request) -> Response:
    """GET /api/export — export traces as CSV or JSON."""
    fmt = request.query_params.get("format", "json")
    if fmt not in ("json", "csv"):
        return JSONResponse({"error": "Invalid format. Use json or csv."}, status_code=400)
    try:
        limit = max(1, min(_MAX_EXPORT_LIMIT, int(request.query_params.get("limit", "500"))))
    except (ValueError, TypeError):
        return JSONResponse({"error": "Invalid limit parameter"}, status_code=400)

    store = request.app.state.trace_store
    if store is None:
        return JSONResponse({"error": "No trace store configured"}, status_code=404)

    records, _ = await store.query(limit=limit)
    rows = [r.model_dump() for r in records]

    if fmt == "csv":
        if not rows:
            return Response("", media_type="text/csv")

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        for row in rows:
            # Flatten nested dicts for CSV
            flat = {}
            for k, v in row.items():
                flat[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
            writer.writerow(flat)
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=traces.csv"},
        )

    return JSONResponse({"traces": rows, "count": len(rows)})


routes = [
    Route("/api/export", export_traces),
]
