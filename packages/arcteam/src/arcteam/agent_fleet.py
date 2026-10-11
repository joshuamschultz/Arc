"""Fleet capability, in the shape an ArcAgent asks for.

An agent runs alone and knows nothing about this package. What it declares is a
seam — ``arcagent.fleet`` — for the few things it cannot do by itself: turn a
handle into a DID, ask who is available for work, and put a notice in another
agent's inbox. This module is arcteam answering that seam.

Everything the agent used to build for itself lives here now: the bus
connection, the roster, the audit chain, the signing. That is the whole point of
the direction — orchestration knows about agents, an agent never knows about
orchestration.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from arcteam.audit import AuditLogger
from arcteam.composition import make_backend, message_signer
from arcteam.harness.enrollment import (
    OperatorKeyResolver,
    default_operator_key_resolver,
    is_eligible,
)
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry, resolve
from arcteam.types import Entity, EntityStatus, EntityType, Message, MsgType

if TYPE_CHECKING:
    from arcteam.workflow.validator import KnownReferences

# The agent asks for three notice kinds; the bus carries many. Mapping them here
# keeps the richer taxonomy out of the agent, which has no use for it.
_NOTICE_KINDS = {
    "task_assigned": MsgType.TASK_ASSIGNED,
    "info": MsgType.INFO,
    "alert": MsgType.ALERT,
}


class FleetDirectoryAdapter:
    """``arcagent.fleet.FleetDirectory`` over the entity registry."""

    def __init__(
        self,
        registry: EntityRegistry,
        *,
        resolve_operator_key: OperatorKeyResolver = default_operator_key_resolver,
    ) -> None:
        self._registry = registry
        # H-040 chokepoint 2: how eligibility re-verifies a foreign member's
        # enrollment before it can reach a routing decision.
        self._resolve_operator_key = resolve_operator_key

    async def resolve(self, ref: str) -> str:
        return await resolve(self._registry, ref)

    async def list_agents(self) -> tuple[Entity, ...]:
        """Only agents that may currently be given work.

        Eligibility is decided here rather than by the caller: an agent has no
        business reading membership state, and a roster filter written twice is
        a roster filter that will disagree with itself.

        H-040 chokepoint 2: a member is eligible only if it is an active agent
        AND its enrollment verifies. A revoked/suspended member, or a foreign
        member whose operator grant no longer verifies, never reaches routing.
        """
        entities = await self._registry.list_entities()
        return tuple(
            entity
            for entity in entities
            if is_eligible(entity, resolve_operator_key=self._resolve_operator_key)
        )

    async def list_users(self) -> dict[str, frozenset[str]]:
        """Each active human user's DID and roles — the only legal escalation targets.

        A workflow may hand a timed-out decision only to a person, or to a role a
        person holds (REQ-622). The filter lives here, beside the agent filter,
        so the agent never reads membership state to build it.
        """
        return {
            entity.did: frozenset(entity.roles)
            for entity in await self._registry.list_entities()
            if entity.type == EntityType.USER and entity.status == EntityStatus.active
        }


class FleetMessengerAdapter:
    """``arcagent.fleet.FleetMessenger`` over the messaging service."""

    def __init__(self, messenger: MessagingService) -> None:
        self._messenger = messenger

    async def send_notice(self, notice: Any) -> None:
        await self._messenger.send(
            Message(
                sender=notice.sender,
                to=list(notice.to),
                msg_type=_NOTICE_KINDS[str(notice.kind)],
                body=notice.body,
                classification=notice.classification,
            )
        )


def _known_references(
    agents: frozenset[str], users: Mapping[str, frozenset[str]] | None
) -> KnownReferences:
    """The authoring roster: agents, plus the human users and the roles they hold.

    ``users=None`` means no roster was read, so both user kinds stay unknown and
    run start — which always asks the live registry — is the check.
    """
    from arcteam.workflow.validator import KnownReferences

    if users is None:
        return KnownReferences(agents=agents)
    return KnownReferences(
        agents=agents,
        users=frozenset(users),
        user_roles=frozenset(role for roles in users.values() for role in roles),
    )


class ArcTeamFleet:
    """The whole of what an agent gains by being orchestrated by arcteam.

    Satisfies ``arcagent.fleet.FleetProvider``. An agent holds one of these or
    it holds nothing, and with nothing every fleet feature reports itself
    unavailable while the agent runs on normally.
    """

    async def open_services(
        self, *, nats_url: str, identity: Any, operator_signer: Any
    ) -> tuple[FleetDirectoryAdapter, FleetMessengerAdapter]:
        return await open_fleet_services(
            nats_url=nats_url, identity=identity, operator_signer=operator_signer
        )

    async def open_run_store(self, *, opener: Any) -> tuple[Any, Any]:
        """The shared workflow run plane.

        A workflow one agent authors is its own; a RUN spans agents, so the run
        plane belongs to the layer that spans them. The control plane's purge
        guard asks it for a run count and the read tools ask it for a workflow's
        runs — neither exists on the raw aggregate store underneath.
        """
        from arcstore.backends import open_backend

        from arcteam.workflow.stores import WorkflowRunStore

        backend = await opener() if opener is not None else open_backend()
        if opener is None:
            await backend.start()
        return WorkflowRunStore(backend), backend

    def open_control_plane(
        self,
        *,
        root: Any,
        tier: str,
        operator_public_key: bytes | None,
        audit: Any,
        known_agents: Any,
        known_users: Any,
        runner: Any,
        runs: Any,
    ) -> tuple[Any, Any] | None:
        """Build the control plane over one agent's bundle root.

        Every argument here is load-bearing and every default is dangerous.
        Omitting ``tier`` makes the store believe it is a personal deployment at
        EVERY tier, so an unsigned definition is never refused. Omitting
        ``operator_public_key`` makes verification fall back to the key embedded
        in the sidecar — trust on first use — so a definition signed by ANY key
        an agent holds reports as signed and verified, which is precisely the
        self-blessing the draft-then-operator-sign lifecycle exists to prevent
        (LLM03/LLM06/ASI04). Neither omission shows on the happy path.
        """
        try:
            from arcteam.workflow import (
                DefinitionStore,
                parse_definition,
                validate_definition,
            )
            from arcteam.workflow.control_plane import WorkflowControlPlane
        except ImportError:
            return None

        definitions = DefinitionStore(
            root=root,
            tier=tier,
            operator_public_key=operator_public_key,
            audit=audit,
        )

        def parse(document: Mapping[str, Any]) -> Any:
            return parse_definition(dict(document))

        def validate(definition: Any, *, pending_files: frozenset[str] = frozenset()) -> Any:
            return validate_definition(
                definition,
                # Read now, not captured: the roster changes while an agent runs.
                known=_known_references(known_agents(), known_users()),
                # THE definition's own bundle, not the directory that holds every
                # bundle. One level too high made every file reference read as
                # missing, so an edit that added no files was rejected for
                # prompts and schemas that were sitting right there.
                bundle_root=root / definition.id,
                pending_files=pending_files,
            )

        plane = WorkflowControlPlane(
            definitions=definitions,
            parse=parse,
            validate=validate,
            runner=runner,
            runs=runs,
            tier=cast(Any, tier),
        )
        return plane, definitions

    def open_definitions(self) -> Any | None:
        """The deployment's signed workflow definitions.

        Read from the shared bundle root rather than any agent's workspace: a
        workflow is a deployment artifact the operator signs, not agent state.
        ``None`` when the engine is not installed or no bundle root exists —
        there is simply nothing to reconcile.
        """
        try:
            from arctrust.paths import workflows_dir

            from arcteam.workflow import DefinitionStore
        except ImportError:
            return None
        root = workflows_dir()
        if not root.is_dir():
            return None
        return DefinitionStore(root=root, tier="personal")


async def open_fleet_services(
    *, nats_url: str, identity: Any, operator_signer: Any
) -> tuple[FleetDirectoryAdapter, FleetMessengerAdapter]:
    """Build one deployment's fleet seams over a single bus connection.

    The registry and the messenger share one connection deliberately: resolving
    a handle and delivering to it must see the same roster and stream state.

    The audit chain is signed by the deployment ``operator_signer`` — the real
    operator authority, never an ephemeral key — or a ``message.sent`` record is
    repudiable and no verifier can validate the chain (SEC-F1, AU-9/10).
    Outbound notices are signed with the agent's own identity (REQ-030).
    """
    backend = await make_backend(nats_url)
    audit = AuditLogger(backend, operator_signer)
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    messenger = MessagingService(backend, registry, audit, signer=message_signer(identity))
    return FleetDirectoryAdapter(registry), FleetMessengerAdapter(messenger)


__all__ = [
    "ArcTeamFleet",
    "FleetDirectoryAdapter",
    "FleetMessengerAdapter",
    "open_fleet_services",
]
