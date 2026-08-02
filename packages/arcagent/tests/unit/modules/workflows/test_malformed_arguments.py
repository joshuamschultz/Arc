"""A malformed tool argument must be repairable, never a crash.

What this pins actually happened on a live fleet: an agent asked in chat to
build a workflow sent ``nodes`` as a list of JSON strings. ``project()`` indexed
straight into it, ``AttributeError`` escaped the tool, the capability layer
reported a crash, and the model — given nothing it could act on — reissued the
same call until the run hit its turn limit. Three runs, no workflow, no error
anybody could read.

Model output is untrusted input to a tool (LLM05). Every one of these tools
takes structured arguments, so every one of them is reachable this way.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from .conftest import RecordingControlPlane

pytestmark = pytest.mark.usefixtures("workflows_state")

_NODE = {"id": "collect", "kind": "agent", "agent": "@sales"}


@pytest.mark.asyncio
class TestNothingCrashesOnAModelShapedMistake:
    async def test_create_accepts_nodes_sent_as_json_strings(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        """The exact shape that killed the live run: a list of JSON strings."""
        from arcagent.modules.workflows.capabilities import workflow_create

        result = json.loads(
            await workflow_create(
                workflow_id="client-update", owner="@sales", nodes=[json.dumps(_NODE)]
            )
        )

        assert "errors" not in result, result
        assert result["status"] == "draft"

    async def test_create_accepts_the_whole_node_list_as_one_json_string(self) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create

        result = json.loads(
            await workflow_create(
                workflow_id="client-update", owner="@sales", nodes=json.dumps([_NODE])
            )
        )

        assert "errors" not in result, result

    async def test_a_shape_that_cannot_be_repaired_returns_a_typed_error(self) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create

        result = json.loads(
            await workflow_create(workflow_id="client-update", owner="@sales", nodes=["not json"])
        )

        assert result["errors"], result
        assert result["errors"][0]["field"] == "nodes"

    async def test_add_node_accepts_a_json_string(self) -> None:
        from arcagent.modules.workflows.capabilities import workflow_add_node, workflow_create

        await workflow_create(workflow_id="client-update", owner="@sales", nodes=[_NODE])
        result = json.loads(
            await workflow_add_node(
                workflow_id="client-update",
                expected_version=1,
                node=json.dumps({"id": "research", "kind": "agent", "needs": ["collect"]}),
            )
        )

        assert "errors" not in result, result

    async def test_edit_node_refuses_a_broken_update_without_crashing(self) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create, workflow_edit_node

        await workflow_create(workflow_id="client-update", owner="@sales", nodes=[_NODE])
        result = json.loads(
            await workflow_edit_node(
                workflow_id="client-update",
                node_id="collect",
                expected_version=1,
                updates=["not", "an", "object"],  # type: ignore[arg-type]
            )
        )

        assert result["errors"], result

    async def test_set_trigger_accepts_a_json_string(self) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create, workflow_set_trigger

        await workflow_create(workflow_id="client-update", owner="@sales", nodes=[_NODE])
        result = json.loads(
            await workflow_set_trigger(
                workflow_id="client-update",
                expected_version=1,
                trigger=json.dumps({"type": "cron", "expression": "0 9 * * MON"}),
            )
        )

        assert "errors" not in result, result


def test_the_coercion_names_the_field_it_refused() -> None:
    """A repair message the model can act on names the argument and the shape."""
    from arcagent.modules.workflows.models import as_objects

    with pytest.raises(ValueError, match="nodes"):
        as_objects(42, "nodes")

    with pytest.raises(ValueError, match="object"):
        as_objects([[1, 2]], "nodes")


def test_a_well_formed_list_passes_through_untouched() -> None:
    from arcagent.modules.workflows.models import as_objects

    nodes: list[Any] = [dict(_NODE)]
    assert as_objects(nodes, "nodes") == nodes


@pytest.mark.asyncio
class TestCompanionFilesTravelWithTheDefinition:
    """A node references `prompts/x.md`; some tool has to be able to write it.

    Without this the agent has to find the bundle directory on disk and write
    the file itself — which is exactly the filesystem hunt that burned a whole
    turn budget instead of building the workflow.
    """

    async def test_create_carries_prompt_and_schema_bodies(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create

        result = json.loads(
            await workflow_create(
                workflow_id="client-update",
                owner="@sales",
                nodes=[{**_NODE, "prompt": "prompts/collect.md"}],
                files={"prompts/collect.md": "Collect the client list."},
            )
        )

        assert "errors" not in result, result
        assert workflows_state.files["prompts/collect.md"] == b"Collect the client list."

    async def test_put_files_adds_them_to_an_existing_bundle(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create, workflow_put_files

        await workflow_create(workflow_id="client-update", owner="@sales", nodes=[_NODE])
        result = json.loads(
            await workflow_put_files(
                workflow_id="client-update",
                expected_version=1,
                files={"schemas/out.json": '{"type": "object"}'},
            )
        )

        assert "errors" not in result, result
        assert workflows_state.files["schemas/out.json"] == b'{"type": "object"}'

    async def test_files_sent_as_a_json_string_still_land(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        """The live shape: the whole files map arrived as one JSON string."""
        from arcagent.modules.workflows.capabilities import workflow_create

        result = json.loads(
            await workflow_create(
                workflow_id="client-update",
                owner="@sales",
                nodes=[_NODE],
                files=json.dumps({"prompts/step1.md": "hello"}),
            )
        )

        assert "errors" not in result, result
        assert workflows_state.files["prompts/step1.md"] == b"hello"

    async def test_a_body_that_is_not_text_is_refused(self) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create

        result = json.loads(
            await workflow_create(
                workflow_id="client-update",
                owner="@sales",
                nodes=[_NODE],
                files={"prompts/collect.md": {"not": "text"}},  # type: ignore[dict-item]
            )
        )

        assert result["errors"][0]["field"] == "files"
