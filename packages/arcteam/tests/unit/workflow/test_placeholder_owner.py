"""A workflow still owned by the template placeholder is refused up front.

``arc workflow new --from <template>`` without ``--owner`` leaves ``@operator``
as owner. No agent answers to it, so a run would wait on nobody. Signing and
starting a run must refuse with a typed error naming the node and the fix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arctrust import generate_keypair

from arcteam.workflow import DefinitionStore, parse_definition, sign_definition
from arcteam.workflow.errors import PlaceholderOwnerError
from arcteam.workflow.ownership import PLACEHOLDER_OWNER

from .conftest import Definition, Node
from .test_runner_frontier import build

FLOW = Definition(
    id="placeholder-flow",
    owner=PLACEHOLDER_OWNER,
    channel="channel://x",
    nodes=(Node(id="draft", kind="agent"), Node(id="approve", kind="gate", gate="human:ok")),
)


async def test_start_run_refuses_a_placeholder_owner(stores: Any, registry: Any) -> None:
    runner = build(stores, registry, FLOW)

    with pytest.raises(PlaceholderOwnerError) as caught:
        await runner.start_run(
            "placeholder-flow", input={}, initiator="operator", initiator_did="did:arc:x/1"
        )

    assert caught.value.node_ids == ("draft",)
    assert "draft" in str(caught.value)
    assert "arc " not in str(caught.value) and "--" not in str(caught.value)
    assert "--owner" in caught.value.cli_hint


async def test_test_run_refuses_a_placeholder_owner_too(stores: Any, registry: Any) -> None:
    runner = build(stores, registry, FLOW)

    with pytest.raises(PlaceholderOwnerError):
        await runner.start_run(
            "placeholder-flow",
            input={},
            initiator="operator",
            initiator_did="did:arc:x/1",
            mode="test",
        )


async def test_a_node_that_names_its_own_agent_is_not_blocked(stores: Any, registry: Any) -> None:
    flow = Definition(
        id="named-flow",
        owner=PLACEHOLDER_OWNER,
        channel="channel://x",
        nodes=(Node(id="draft", kind="agent", agent="@sales"),),
    )
    runner = build(stores, registry, flow)

    run = await runner.start_run(
        "named-flow", input={}, initiator="operator", initiator_did="did:arc:x/1"
    )

    assert run.status == "running"


def test_signing_refuses_a_placeholder_owner(tmp_path: Path) -> None:
    keypair = generate_keypair()
    store = DefinitionStore(
        tmp_path / "workflows", tier="personal", operator_public_key=keypair.public_key
    )
    document: dict[str, Any] = {
        "workflow": {"id": "p", "owner": PLACEHOLDER_OWNER},
        "node": [{"id": "gather", "kind": "tool", "tool": "noop"}],
    }
    store.save_draft(parse_definition(document), actor_did="did:arc:x/1", expected_version=None)

    with pytest.raises(PlaceholderOwnerError) as caught:
        sign_definition(store, "p", signer_did="did:arc:op", private_key=keypair.private_key)

    assert caught.value.node_ids == ("gather",)
    assert not (tmp_path / "workflows" / "p" / "workflow.toml.arcsig").exists()
