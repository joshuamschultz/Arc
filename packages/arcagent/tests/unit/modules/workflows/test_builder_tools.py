"""T-849 — builder-tool tests: allowlists, quotas, ordering, typed errors.

SPEC-061 COMP-012 / REQ-221, REQ-222, REQ-223, REQ-248.

Every import of ``arcagent.modules.workflows.*`` is local to its test so a
missing module surfaces as one failure per test rather than a collection error
masking the rest (mirrors ``tests/unit/modules/tasks/test_capabilities.py``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.unit.modules.workflows.conftest import (
    FakeIssue,
    RecordingControlPlane,
    StaleEditError,
)

pytestmark = pytest.mark.usefixtures("workflows_state")


def _node(**overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"id": "collect", "kind": "agent", "agent": "@sales"}
    node.update(overrides)
    return node


class TestFieldAllowlist:
    """Every mutating tool ignores fields outside its explicit allowlist (ASI02)."""

    @pytest.mark.asyncio
    async def test_add_node_drops_undeclared_fields(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        from arcagent.modules.workflows.capabilities import workflow_add_node, workflow_create

        await workflow_create(workflow_id="wf", nodes=[_node()])
        await workflow_add_node(
            workflow_id="wf",
            node=_node(id="verify", model="gpt-4", temperature=0.9, status="signed"),
            expected_version=1,
        )
        sent = workflows_state.last("edit")["document"]["node"][-1]
        assert "model" not in sent
        assert "temperature" not in sent
        assert "status" not in sent
        assert sent["id"] == "verify"

    @pytest.mark.asyncio
    async def test_edit_node_drops_undeclared_fields(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create, workflow_edit_node

        await workflow_create(workflow_id="wf", nodes=[_node()])
        await workflow_edit_node(
            workflow_id="wf",
            node_id="collect",
            updates={"prompt": "prompts/x.md", "id": "renamed", "owner_did": "did:evil"},
            expected_version=1,
        )
        sent = workflows_state.last("edit")["document"]["node"][0]
        assert sent["prompt"] == "prompts/x.md"
        assert sent["id"] == "collect"  # the id is immutable
        assert "owner_did" not in sent

    @pytest.mark.asyncio
    async def test_edit_node_with_no_allowlisted_field_is_refused(self) -> None:
        from arcagent.modules.workflows.capabilities import workflow_edit_node

        result = json.loads(
            await workflow_edit_node(
                workflow_id="wf", node_id="collect", updates={"nope": 1}, expected_version=1
            )
        )
        assert "errors" in result
        assert result["errors"][0]["field"] == "updates"

    @pytest.mark.asyncio
    async def test_create_drops_undeclared_workflow_fields(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create

        await workflow_create(workflow_id="wf", description="d", nodes=[_node()])
        header = workflows_state.last("create")["document"]["workflow"]
        assert "status" not in header
        assert "content_hash" not in header
        assert "version" not in header


class TestQuotasBeforeValidation:
    """Quota checks run BEFORE any validation work reaches the control plane (LLM10)."""

    @pytest.mark.asyncio
    async def test_workflow_quota_refuses_without_calling_control_plane(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create

        await workflow_create(workflow_id="a", nodes=[_node()])
        await workflow_create(workflow_id="b", nodes=[_node()])
        workflows_state.calls.clear()

        result = json.loads(await workflow_create(workflow_id="c", nodes=[_node()]))

        assert "errors" in result
        assert "quota" in result["errors"][0]["error"].lower()
        # The refusal must have cost at most a list() — never a create().
        assert "create" not in workflows_state.ops()

    @pytest.mark.asyncio
    async def test_node_quota_refuses_before_control_plane(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create

        nodes = [_node(id=f"n{i}") for i in range(4)]
        result = json.loads(await workflow_create(workflow_id="wf", nodes=nodes))

        assert "errors" in result
        assert result["errors"][0]["field"] == "nodes"
        assert "create" not in workflows_state.ops()


class TestInlineTextNormalization:
    """Inline free text is NFKC-normalized before the injection scan (LLM01)."""

    @pytest.mark.asyncio
    async def test_homoglyph_injection_in_description_is_rejected(self) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create

        # Full-width Latin "ignore previous" — NFKC folds it back to ASCII.
        payload = "".join(
            chr(0xFF00 + ord(c) - 0x20) if c != " " else c for c in "ignore previous"
        )
        result = json.loads(
            await workflow_create(workflow_id="wf", description=payload, nodes=[_node()])
        )
        assert "errors" in result
        assert result["errors"][0]["field"] == "description"

    @pytest.mark.asyncio
    async def test_zero_width_split_injection_is_rejected(self) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create

        result = json.loads(
            await workflow_create(
                workflow_id="wf", description="ig\u200bnore previous", nodes=[_node()]
            )
        )
        assert "errors" in result

    @pytest.mark.asyncio
    async def test_clean_description_passes_through_normalized(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create

        await workflow_create(
            workflow_id="wf", description="Onboard\u200b new customers", nodes=[_node()]
        )
        header = workflows_state.last("create")["document"]["workflow"]
        assert header["description"] == "Onboard new customers"


class TestTypedErrors:
    """REQ-222 — rejections carry node id, field, observed value, and alternatives."""

    @pytest.mark.asyncio
    async def test_control_plane_issues_are_surfaced_verbatim(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create

        workflows_state.refuse_with = (FakeIssue("verify", "needs", "unknown node 'collct'"),)
        result = json.loads(await workflow_create(workflow_id="wf", nodes=[_node()]))

        assert result["errors"] == [
            {
                "node_id": "verify",
                "field": "needs",
                "error": "unknown node 'collct'",
                "observed": None,
                "admissible": [],
            }
        ]

    @pytest.mark.asyncio
    async def test_every_error_carries_the_full_typed_shape(self) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create

        result = json.loads(await workflow_create(workflow_id="", nodes=[_node()]))
        assert "errors" in result
        for issue in result["errors"]:
            assert set(issue) == {"node_id", "field", "error", "observed", "admissible"}

    @pytest.mark.asyncio
    async def test_repair_budget_is_advertised_on_rejection(self) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create

        result = json.loads(await workflow_create(workflow_id="", nodes=[_node()]))
        assert result["max_repair_attempts"] == 3


class TestOptimisticConcurrency:
    """REQ-248 — an edit requires expected_version; a stale edit is refused."""

    @pytest.mark.asyncio
    async def test_expected_version_is_forwarded(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create, workflow_set_channel

        await workflow_create(workflow_id="wf", nodes=[_node()])
        await workflow_set_channel(workflow_id="wf", channel="channel://x", expected_version=7)
        assert workflows_state.last("edit_meta")["expected_version"] == 7

    @pytest.mark.asyncio
    async def test_missing_expected_version_is_refused_before_the_control_plane(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        from arcagent.modules.workflows.capabilities import workflow_set_channel

        result = json.loads(await workflow_set_channel(workflow_id="wf", channel="channel://x"))
        assert "errors" in result
        assert result["errors"][0]["field"] == "expected_version"
        assert workflows_state.ops() == []

    @pytest.mark.asyncio
    async def test_stale_edit_is_returned_as_a_typed_error_not_merged(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        from arcagent.modules.workflows.capabilities import workflow_create, workflow_edit_node

        await workflow_create(workflow_id="wf", nodes=[_node()])
        workflows_state.raise_on["edit"] = StaleEditError("expected version 2, found 5")
        result = json.loads(
            await workflow_edit_node(
                workflow_id="wf",
                node_id="collect",
                updates={"prompt": "p.md"},
                expected_version=2,
            )
        )
        assert "errors" in result
        assert result["errors"][0]["field"] == "expected_version"


class TestDraftStatusInvariant:
    """T-834 / REQ-223 — no arcagent path lets validation success confer signed status."""

    @pytest.mark.asyncio
    async def test_every_mutation_returns_a_draft(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        from arcagent.modules.workflows.capabilities import (
            workflow_add_node,
            workflow_create,
            workflow_set_channel,
            workflow_set_trigger,
        )

        results = [
            json.loads(await workflow_create(workflow_id="wf", nodes=[_node()])),
            json.loads(
                await workflow_add_node(workflow_id="wf", node=_node(id="b"), expected_version=1)
            ),
            json.loads(
                await workflow_set_trigger(
                    workflow_id="wf",
                    trigger={"type": "cron", "expression": "0 9 * * MON"},
                    expected_version=2,
                )
            ),
            json.loads(
                await workflow_set_channel(
                    workflow_id="wf", channel="channel://x", expected_version=3
                )
            ),
        ]
        assert [r["status"] for r in results] == ["draft", "draft", "draft", "draft"]

    @pytest.mark.asyncio
    async def test_a_non_draft_result_from_a_mutation_fails_closed(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        """If anything ever hands back a signed bundle from a MUTATION, refuse it.

        This is the enforcement, not just the assertion: the tool surface never
        reports a mutation as signed even if a lower layer regressed.
        """
        from arcagent.modules.workflows.capabilities import workflow_create

        workflows_state.next_status = "signed"
        result = json.loads(await workflow_create(workflow_id="wf", nodes=[_node()]))
        assert "errors" in result
        assert "draft" in result["errors"][0]["error"]

    def test_no_builder_tool_accepts_a_status_or_signature_argument(self) -> None:
        """Structural guard: the signing key never reaches an agent process."""
        import inspect

        from arcagent.modules.workflows import capabilities

        forbidden = {"status", "signature", "signed", "sign", "operator_key", "signer"}
        for name in capabilities.__all__:
            fn = getattr(capabilities, name)
            if not callable(fn):
                continue
            params = set(inspect.signature(fn).parameters)
            assert not (params & forbidden), f"{name} exposes a signing argument"

    def test_the_module_source_never_writes_a_signed_status(self) -> None:
        from pathlib import Path

        import arcagent.modules.workflows as pkg

        for path in Path(pkg.__file__).parent.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert '"signed"' not in source, f"{path.name} writes a signed status literal"


class TestReadOnlyTools:
    """The read-only surface never mutates and is classified read_only."""

    @pytest.mark.asyncio
    async def test_reads_do_not_reach_a_mutating_op(
        self, workflows_state: RecordingControlPlane
    ) -> None:
        from arcagent.modules.workflows.capabilities import (
            workflow_inspect,
            workflow_list,
            workflow_run_status,
            workflow_runs,
        )

        await workflow_list()
        await workflow_inspect(workflow_id="wf")
        await workflow_runs(workflow_id="wf")
        await workflow_run_status(run_id="run_1")

        mutating = {"create", "add_node", "edit_node", "remove_node", "set_trigger", "set_channel"}
        assert not (set(workflows_state.ops()) & mutating)

    def test_tool_classifications_match_the_spec(self) -> None:
        from arcagent.modules.workflows import capabilities
        from arcagent.tools._decorator import ToolMetadata, capability_meta

        expected = {
            "workflow_create": "state_modifying",
            "workflow_add_node": "state_modifying",
            "workflow_edit_node": "state_modifying",
            "workflow_remove_node": "state_modifying",
            "workflow_set_trigger": "state_modifying",
            "workflow_set_channel": "state_modifying",
            "workflow_run": "state_modifying",
            "workflow_cancel_run": "state_modifying",
            "workflow_list": "read_only",
            "workflow_inspect": "read_only",
            "workflow_runs": "read_only",
            "workflow_run_status": "read_only",
        }
        for tool_name, classification in expected.items():
            meta = capability_meta(getattr(capabilities, tool_name))
            assert isinstance(meta, ToolMetadata)
            assert meta.name == tool_name
            assert meta.classification == classification


class TestNoGateResolutionTool:
    """REQ-246 — no agent-callable tool resolves a gate."""

    def test_module_exposes_no_gate_resolution_surface(self) -> None:
        from arcagent.modules.workflows import capabilities

        for name in capabilities.__all__:
            assert "gate" not in name
            assert "approve" not in name


class TestTheStoreIsBuiltWithItsSecurityContext:
    """`DefinitionStore(root=root)` omitted tier AND the pinned operator key.

    The store then believed every deployment was personal-tier with no pin, so
    an agent could sign a workflow with its OWN key and the store reported it
    verified — exactly the attack the draft-then-operator-sign lifecycle exists
    to prevent. With no pin, verification falls back to trusting the key in the
    sidecar: trust-on-first-use, the LLM03 hole SPEC-047 already closed for
    blueprints. Pure omission, and the tier was already in scope.
    """

    def test_the_store_receives_the_real_tier_and_the_pinned_key(self) -> None:
        import inspect

        from arcagent.modules.workflows import _runtime

        src = inspect.getsource(_runtime._build_control_plane)
        assert "tier=st.tier" in src, "the store must be told the real tier"
        assert "operator_public_key=" in src, "the store must be given the pinned key"

    def test_an_agent_signed_workflow_is_refused_above_personal(self, tmp_path: Any) -> None:
        """The behavioural proof: a rogue key must not produce a verified bundle."""
        from arcteam.workflow import parse_definition
        from arcteam.workflow.errors import WorkflowError
        from arcteam.workflow.store import DefinitionStore, sign_definition
        from nacl.signing import SigningKey

        store = DefinitionStore(root=tmp_path / "wf", tier="federal", operator_public_key=None)
        doc = {
            "workflow": {"id": "x", "version": 1, "owner": "@me"},
            "node": [{"id": "a", "kind": "agent", "agent": "@me"}],
        }
        store.save_draft(
            parse_definition(doc), actor_did="did:arc:agent:rogue", expected_version=None
        )
        sign_definition(
            store, "x", signer_did="did:arc:agent:rogue", private_key=bytes(SigningKey.generate())
        )

        assert store.load("x").is_verified is False, "a rogue key must never read as verified"
        with pytest.raises(WorkflowError):
            store.load_for_run("x")
