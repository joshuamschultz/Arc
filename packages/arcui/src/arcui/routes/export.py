"""Export route — /api/export (CSV/JSON).

SPEC-026 FR-5: traces are exported from the arcstore mirror (``app.state.observe``).
"""

from __future__ import annotations

import csv
import io
import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from arcui.query_validators import safe_choice, safe_int
from arcui.schemas import ExportTracesResponse

_MAX_EXPORT_LIMIT = 10000


async def export_traces(request: Request) -> Response:
    """GET /api/export — export traces as CSV or JSON."""
    fmt, err = safe_choice(
        request.query_params.get("format", "json"),
        ("json", "csv"),
        error_label="Invalid format. Use json or csv.",
    )
    if err is not None:
        return err
    limit, err = safe_int(
        request.query_params.get("limit"),
        default=500,
        min_=1,
        max_=_MAX_EXPORT_LIMIT,
        error_label="Invalid limit parameter",
    )
    if err is not None:
        return err

    rows = await request.app.state.observe.traces(limit=limit)

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

    return JSONResponse(ExportTracesResponse(traces=rows, count=len(rows)).model_dump(mode="json"))


routes = [
    Route("/api/export", export_traces),
]
