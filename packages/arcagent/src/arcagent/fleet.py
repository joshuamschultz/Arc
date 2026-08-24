"""The seam an agent uses to reach a fleet it may not be part of.

ArcAgent runs alone. Everything about *other* agents — who they are, how to
address one, how to put a notice in another's inbox — belongs to the
orchestration layer above it, and an agent running by itself simply does not
have it.

So this file is the contract, not an implementation: the shapes an agent needs
from a fleet, and nothing about how a fleet is built, connected or signed. The
orchestration layer supplies objects that satisfy these Protocols when it
composes an agent. With none supplied the seam is absent, every fleet feature
reports itself unavailable, and the rest of the agent is unaffected.

The direction is one-way and load-bearing: the fleet layer knows about agents,
an agent never knows about the fleet layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class FleetNoticeKind(StrEnum):
    """Why one agent is putting something in another inbox.

    Deliberately small: an agent only ever hands work over, reports something
    routine, or raises something that needs attention. Richer message taxonomies
    belong to the layer that owns the bus.
    """

    TASK_ASSIGNED = "task_assigned"
    INFO = "info"
    ALERT = "alert"


class FleetNotice(BaseModel):
    """One outbound message, in the only shape an agent needs to express.

    ``classification`` travels on the envelope rather than being inferred at the
    delivery site: the no-write-down check downstream is only as good as the
    label it is given, and an unclassified default would leave it inert however
    sensitive the contents (ASI07).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sender: str = Field(min_length=1)
    to: tuple[str, ...] = Field(min_length=1)
    kind: FleetNoticeKind
    body: str = ""
    classification: str = "unclassified"


@runtime_checkable
class FleetMember(Protocol):
    """Another addressable agent, in the only terms this agent reasons about.

    Enough to choose one for a piece of work and to name it afterwards. An agent
    never sees membership, status or roster mechanics — the fleet layer decides
    who is eligible before any member reaches here.
    """

    @property
    def did(self) -> str: ...

    @property
    def handle(self) -> str:
        """The addressable name, without its ``@``."""
        ...

    @property
    def name(self) -> str: ...

    @property
    def capabilities(self) -> Sequence[str]: ...


class FleetDirectory(Protocol):
    """Address resolution and the eligible roster."""

    async def resolve(self, ref: str) -> str:
        """The canonical DID behind ``@handle``, ``agent://handle`` or a raw DID.

        A ref naming nobody raises ``ValueError``. An unknown address is a
        caller mistake the agent reports back, never a silent drop.
        """
        ...

    async def list_agents(self) -> tuple[FleetMember, ...]:
        """Agents that may currently be given work. Filtering is the fleet's call."""
        ...


class FleetMessenger(Protocol):
    """Delivery of one notice to another inbox."""

    async def send_notice(self, notice: FleetNotice) -> None: ...


class FleetProvider(Protocol):
    """Everything an agent gets by being part of a fleet, and nothing more.

    An agent does not stand up a bus, a roster, an audit chain or a shared run
    plane of its own — that is the orchestration layer's job, and an agent alone
    has no business doing it. It names the deployment it belongs to and its own
    identity, and receives the seams back.

    One provider rather than a bag of separate hooks: what an agent gains from
    being orchestrated is a single thing, and it either has it or it does not.
    """

    async def open_services(
        self, *, nats_url: str, identity: object, operator_signer: object
    ) -> tuple[FleetDirectory, FleetMessenger]:
        """Address resolution and delivery for this deployment.

        ``operator_signer`` is passed because delivery is audited under the
        deployment's real operator authority; an agent must never be able to
        sign that chain with a key of its own.
        """
        ...

    async def open_run_store(self, *, opener: object) -> tuple[Any, Any]:
        """The shared workflow run plane, and the backend that owns it.

        A workflow an agent authors alone is its own; a workflow RUN spans
        agents, so the run plane belongs to the layer that spans them.
        """
        ...

    def open_control_plane(
        self,
        *,
        root: Any,
        tier: str,
        operator_public_key: bytes | None,
        audit: Any,
        known_agents: Any,
        runner: Any,
        runs: Any,
    ) -> tuple[Any, Any] | None:
        """The workflow control plane and its definition store, or None.

        Running one workflow across many agents is the orchestration layer's
        whole job, so it builds the plane; the agent supplies only what is its
        own — where its bundles live, its tier, the operator key it verifies
        against, and its audit hook. ``known_agents`` is read at validation time
        rather than captured, because the roster changes while an agent runs.

        None when no workflow engine is installed: authoring then reports itself
        unavailable and every other capability is untouched.
        """
        ...

    def open_definitions(self) -> Any | None:
        """The deployment's signed workflow definitions, or None if there are none.

        An agent reads these only to learn which of its own schedules a workflow
        expects it to keep. The definitions themselves are deployment artifacts
        the operator signs, so the layer that owns them hands them over.
        """
        ...


__all__ = [
    "FleetDirectory",
    "FleetMember",
    "FleetMessenger",
    "FleetNotice",
    "FleetNoticeKind",
    "FleetProvider",
]
