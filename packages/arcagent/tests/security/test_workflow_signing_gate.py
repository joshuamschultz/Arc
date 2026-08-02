"""The signing gate, exercised through the AGENT-authored surface (REQ-225).

SPEC-061's whole draft-then-operator-sign lifecycle rests on one claim: an agent
can author a workflow but cannot bless it. If the authoring process could also
sign, a prompt injection could author an exfiltration pipeline and then mark it
trusted (LLM06/ASI04) — which is the reason signing is an out-of-band operator
command with a key that never enters an agent process.

That claim is only true if the definition store the agent surface builds is
constructed with the deployment tier AND the pinned operator key. Neither is a
behaviour you can see on the happy path: a correctly signed workflow on a
personal deployment behaves identically whether or not they were passed. Only an
UNSIGNED definition and a FOREIGN-signed one tell the two apart, so those are the
two cases here, and both go through the real ``DefinitionStore`` the module
builds for itself rather than one the test constructs.

Reported by the arcteam workstream against the real constructor; these are the
regression tests that were missing when it slipped.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arcteam.workflow import UnsignedWorkflowError, sign_definition
from arctrust import AgentIdentity, OperatorKey


def _document() -> dict[str, Any]:
    return {
        "workflow": {"id": "onboarding", "description": "intake", "owner": "@sales"},
        "node": [{"id": "collect", "kind": "agent", "agent": "@sales"}],
    }


def _sign_with(store: Any, key: OperatorKey) -> Any:
    """Sign the bundle out of band with ``key``, as ``arc workflow sign`` does.

    Deliberately the REAL signing path rather than a hand-written sidecar: the
    attack under test is a valid, correctly-formed signature carrying the wrong
    key, and a forged sidecar would prove something weaker.
    """
    return sign_definition(
        store,
        "onboarding",
        signer_did=f"did:arc:key/{key.public_key.hex()[:16]}",
        private_key=key.seed,
    )


@pytest.fixture
def operator() -> OperatorKey:
    """The deployment operator authority — the ONLY key a signature may carry."""
    return OperatorKey.generate()


@pytest.fixture
def rogue() -> OperatorKey:
    """Any other Ed25519 key. An agent could hold one; it must buy nothing."""
    return OperatorKey.generate()


def _configure(tmp_path: Path, tier: str, operator: OperatorKey) -> Any:
    """Bring up the workflows runtime exactly as the agent lifecycle does."""
    from arcagent.modules.workflows import _runtime

    _runtime.reset()
    _runtime.configure(
        config={"enabled": True},
        workspace=tmp_path,
        identity=AgentIdentity.generate(org="local", agent_type="agent"),
        operator_signer=operator.into_signer(),
        tier=tier,
    )
    return _runtime


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    from arcagent.modules.workflows import _runtime

    yield
    _runtime.reset()


async def _store(tmp_path: Path, tier: str, operator_signer: Any) -> Any:
    """The definition store the MODULE builds — never one the test constructs."""
    runtime = _configure(tmp_path, tier, operator_signer)
    await runtime.ensure_control_plane()
    return runtime.state().definitions


@pytest.mark.asyncio
class TestTierReachesTheStore:
    """A store told nothing believes it is personal-tier — at every deployment."""

    @pytest.mark.parametrize("tier", ["enterprise", "federal"])
    async def test_store_is_constructed_with_the_deployment_tier(
        self, tmp_path: Path, operator: Any, tier: str
    ) -> None:
        store = await _store(tmp_path, tier, operator)
        assert store.tier == tier

    @pytest.mark.parametrize("tier", ["enterprise", "federal"])
    async def test_unsigned_definition_is_refused_above_personal(
        self, tmp_path: Path, operator: Any, tier: str
    ) -> None:
        """REQ-225 — the refusal that never fired."""
        store = await _store(tmp_path, tier, operator)
        from arcteam.workflow import parse_definition

        store.save_draft(
            parse_definition(_document()), actor_did="did:arc:agent", expected_version=None
        )

        with pytest.raises(UnsignedWorkflowError):
            store.load_for_run("onboarding")

    async def test_unsigned_definition_still_runs_at_personal(
        self, tmp_path: Path, operator: Any
    ) -> None:
        """Tier is stringency, not a gate — personal still runs, and audits it."""
        store = await _store(tmp_path, "personal", operator)
        from arcteam.workflow import parse_definition

        store.save_draft(
            parse_definition(_document()), actor_did="did:arc:agent", expected_version=None
        )
        assert store.load_for_run("onboarding").definition.id == "onboarding"


@pytest.mark.asyncio
class TestOperatorKeyIsPinned:
    """A signature is trusted for WHOSE key it carries, never merely for verifying."""

    async def test_rogue_signed_definition_is_not_verified(
        self, tmp_path: Path, operator: Any, rogue: Any
    ) -> None:
        """The attack: an agent holding any key self-blesses its own workflow."""
        store = await _store(tmp_path, "personal", operator)
        from arcteam.workflow import parse_definition

        store.save_draft(
            parse_definition(_document()), actor_did="did:arc:agent", expected_version=None
        )
        _sign_with(store, rogue)

        reloaded = store.load("onboarding")
        assert reloaded.is_verified is False
        assert reloaded.status != "signed"

    @pytest.mark.parametrize("tier", ["enterprise", "federal"])
    async def test_rogue_signed_definition_is_refused_above_personal(
        self, tmp_path: Path, operator: Any, rogue: Any, tier: str
    ) -> None:
        store = await _store(tmp_path, tier, operator)
        from arcteam.workflow import parse_definition

        store.save_draft(
            parse_definition(_document()), actor_did="did:arc:agent", expected_version=None
        )
        _sign_with(store, rogue)

        with pytest.raises(UnsignedWorkflowError):
            store.load_for_run("onboarding")

    async def test_operator_signed_definition_is_verified(
        self, tmp_path: Path, operator: Any
    ) -> None:
        """The guard must not break the case it exists to permit."""
        store = await _store(tmp_path, "federal", operator)
        from arcteam.workflow import parse_definition

        store.save_draft(
            parse_definition(_document()), actor_did="did:arc:agent", expected_version=None
        )
        _sign_with(store, operator)

        reloaded = store.load("onboarding")
        assert reloaded.is_verified is True
        assert store.load_for_run("onboarding").definition.id == "onboarding"


@pytest.mark.asyncio
class TestAuditReachesTheStore:
    """``workflow.signed`` and ``workflow.unsigned_run_permitted`` are store-only.

    Nothing else in the system can emit them, so an unwired hook means an agent
    self-signing a workflow leaves no record anywhere.
    """

    async def test_unsigned_run_at_personal_is_audited(
        self, tmp_path: Path, operator: Any
    ) -> None:
        from arcteam.workflow import parse_definition

        from arcagent.modules.workflows import _runtime

        events: list[tuple[str, dict[str, Any]]] = []
        _configure(tmp_path, "personal", operator)
        _runtime.state().audit_hook = lambda event, payload: events.append((event, payload))
        await _runtime.ensure_control_plane()
        store = _runtime.state().definitions

        store.save_draft(
            parse_definition(_document()), actor_did="did:arc:agent", expected_version=None
        )
        store.load_for_run("onboarding")

        assert any(event == "workflow.unsigned_run_permitted" for event, _ in events)


class TestConstructionIsNotDefaulted:
    """A structural guard: the omission that caused this was silent by nature."""

    def test_the_store_is_never_built_on_the_constructor_defaults(self) -> None:
        from pathlib import Path as _Path

        from arcagent.modules.workflows import _runtime

        source = _Path(_runtime.__file__).read_text(encoding="utf-8")
        _, _, construction = source.partition("DefinitionStore(")
        head = construction[: construction.index("\n    )")]
        for required in ("tier=", "operator_public_key=", "audit="):
            assert required in head, (
                f"DefinitionStore is built without {required} — its default makes the "
                "deployment look personal-tier with no pinned operator key, which "
                "silently disables REQ-225 at every tier"
            )
