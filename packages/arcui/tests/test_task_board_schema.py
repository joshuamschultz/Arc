"""Fleet board has a stricter wire contract than legacy agent task lists."""

import pytest
from pydantic import ValidationError

from arcui.schemas import TaskBoardResponse


def _board() -> dict:
    return {
        "tasks": [],
        "facets": {
            "statuses": {},
            "priorities": {},
            "owners": {},
            "tags": {},
            "total": 0,
            "blocked": 0,
            "done_today": 0,
            "avg_done_seconds": None,
        },
        "projections": {
            "a": {
                "blocked": False,
                "dependencies": {},
                "dependency_total": 0,
                "children": [],
                "child_total": 0,
                "child_done": 0,
            }
        },
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda board: board["facets"].update(total="0"),
        lambda board: board["facets"].update(blocked=-1),
        lambda board: board["projections"]["a"].update(blocked="false"),
        lambda board: board["projections"]["a"].update(child_done=1),
    ],
)
def test_board_schema_rejects_malformed_fields(mutate) -> None:
    body = _board()
    mutate(body)
    with pytest.raises(ValidationError):
        TaskBoardResponse.model_validate(body)
