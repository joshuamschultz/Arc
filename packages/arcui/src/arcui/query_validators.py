"""Query-param parsing helpers for arcui routes (Phase 6 §9.2).

These helpers extract the repeated "parse + clamp + 400 on failure" and
"check membership + 400 on miss" patterns from the route handlers so
each callsite shrinks to a single line.

**Behavior contract:** every helper produces outputs that are
byte-identical to the most-careful existing caller for the same input.
No silent tightening — if a current route was lenient, the helper
preserves that leniency. Pure refactor; no new validation, no new
failure modes, no exception narrowing.

Shape:
- ``safe_int`` returns ``(clamped_int, JSONResponse | None)``. The
  caller checks the second tuple slot and short-circuits the route
  with that response on parse failure.
- ``safe_choice`` returns ``(value, JSONResponse | None)``. Same
  short-circuit pattern.
- ``parse_pagination`` returns ``(page, page_size, JSONResponse | None)``.

Error response shape mirrors the existing routes exactly:
``JSONResponse({"error": <label>}, status_code=400)``.
"""

from __future__ import annotations

from collections.abc import Iterable

from starlette.datastructures import QueryParams
from starlette.responses import JSONResponse


def safe_int(
    raw: str | None,
    *,
    default: int,
    min_: int,
    max_: int,
    error_label: str,
) -> tuple[int, JSONResponse | None]:
    """Parse + clamp an integer query-param value.

    Returns ``(clamped_value, None)`` on success.
    Returns ``(0, JSONResponse({"error": error_label}, status_code=400))``
    when ``raw`` is non-numeric — matches the existing
    ``except ValueError: return JSONResponse(..., 400)`` pattern.

    When ``raw`` is ``None`` (param omitted), uses ``default`` and
    still clamps to ``[min_, max_]``.
    """
    if raw is None:
        return max(min_, min(max_, default)), None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return 0, JSONResponse({"error": error_label}, status_code=400)
    return max(min_, min(max_, parsed)), None


def safe_choice(
    value: str,
    choices: Iterable[str],
    *,
    error_label: str,
) -> tuple[str, JSONResponse | None]:
    """Validate that *value* is one of *choices*.

    Returns ``(value, None)`` if accepted.
    Returns ``(value, JSONResponse({"error": error_label}, status_code=400))``
    when *value* is not in *choices* — matches the existing
    ``if value not in {…}: return JSONResponse(..., 400)`` pattern.
    """
    if value in choices:
        return value, None
    return value, JSONResponse({"error": error_label}, status_code=400)


def parse_pagination(qp: QueryParams) -> tuple[int, int, JSONResponse | None]:
    """Parse ``page`` (1-based) and ``page_size`` (1..200) query params.

    Returns ``(page, page_size, None)`` on success.
    Returns ``(0, 0, JSONResponse({"error": "Invalid pagination"}, 400))``
    on parse failure — matches the existing
    ``arcui.routes.agent_detail.sessions._parse_pagination`` shape.
    """
    raw_page = qp.get("page", "1")
    raw_size = qp.get("page_size", "50")
    try:
        page = max(1, int(raw_page))
        page_size = max(1, min(200, int(raw_size)))
    except ValueError:
        return 0, 0, JSONResponse({"error": "Invalid pagination"}, status_code=400)
    return page, page_size, None
