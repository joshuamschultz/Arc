"""One tool name serving every granted connection of a bundle — routed, never trusted.

A bundle connected more than once (three mailboxes) serves the same tool
names for each connection. Registering them per connection meant the first
connection took every name and the rest served nothing — and because several
verbs took an ``--account`` argument straight onto argv, an agent granted only
the first mailbox could act as any other mailbox signed in on the host, while
the audit named the first. :class:`RoutedAttachment` replaces that with one
registered tool per name whose every call is resolved, not believed:

* the **allowed set** is the connections of this bundle granted to THIS agent —
  the only ones attached here — and a grant is re-read at call time, so a
  revocation beats the next reconcile;
* the selector argument (``[tools.routing].argument``) is optional when exactly
  one connection is allowed, and an ambiguous omission fails closed listing the
  choices; a given value must equal one connection's stored field under the
  declared comparison (``email``: Unicode NFKC plus case folding, and nothing
  else forgiven — no whitespace, no alias);
* the selector is removed before the call reaches the attachment, and the chosen
  connection's OWN attachment runs it — with its own placed environment
  (account, OAuth client) and its own approval binding. Nothing the model typed
  becomes the account, and no other argument may carry a flag or environment
  assignment the manifest's ``refuse_values`` names;
* a connection signed in read-only (``[tools.read_only]``) refuses every tool
  whose classification is not ``read_only``, with a sentence, before anything runs;
* every routed call and every denial is audited with the REAL connection, and the
  result the model reads names the connection and account that answered.

Generic by construction: the manifest names the selector, the field and the
comparison, so any second account of any bundle reuses this unchanged.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.extension.attachment import (
    ExtensionAttachment,
    ProbeResult,
    Requirement,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from arcagent.extension.manifest import ToolRouting

#: A requested selector longer than this is not an account; it is only ever
#: recorded truncated.
_MAX_RECORDED = 256

_ROUTED_ACTION = "connector.account.routed"
_DENIED_ACTION = "connector.account.denied"


@dataclass(frozen=True)
class RoutedMember:
    """One granted connection behind a routed tool.

    ``attachment`` is the connection's own, already bound to its approval mode.
    ``selector`` is its stored field value — what a request is matched against
    and, via the connection's placed environment, what the call acts as.
    ``approved`` is the tools this connection's approved contract allows.
    """

    instance: str
    attachment: ExtensionAttachment
    selector: str
    approved: frozenset[str]
    read_only: bool = False


class RoutedAttachment:
    """Satisfies :class:`~arcagent.extension.attachment.ExtensionAttachment` over members.

    Args:
        extension: The bundle name, recorded on every audit event.
        routing: The manifest's ``[tools.routing]``.
        members: Every connection of this bundle attached for this agent.
        specs: The tools to register — the union the members serve, annotated
            from the manifest. The selector is added to each schema here.
        agent_did: The calling agent — the only caller this registry serves.
        tier: Stamped on every audit event.
        sink: Where routing verdicts are recorded.
        grant_active: Re-reads whether this agent still holds a connection.
    """

    def __init__(
        self,
        *,
        extension: str,
        routing: ToolRouting,
        members: Sequence[RoutedMember],
        specs: Sequence[ToolSpec],
        agent_did: str,
        tier: str,
        sink: AuditSink,
        grant_active: Callable[[str], Awaitable[bool]],
    ) -> None:
        if not members:
            raise ValueError("a routed attachment needs at least one connection")
        self._extension = extension
        self._routing = routing
        self._members = tuple(members)
        self._specs = {spec.name: spec for spec in specs}
        self._agent_did = agent_did
        self._tier = tier
        self._sink = sink
        self._grant_active = grant_active
        self._refuse = re.compile(routing.refuse_values) if routing.refuse_values else None

    # --- the hook contract ---------------------------------------------------

    def requirements(self) -> list[Requirement]:
        """The members share one bundle, so they share its requirements."""
        return self._members[0].attachment.requirements()

    async def probe(self) -> ProbeResult:
        """The first member's answer; each connection is probed on its own card."""
        return await self._members[0].attachment.probe()

    async def describe_tools(self) -> list[ToolSpec]:
        """Every tool once, each schema carrying the optional selector."""
        return self.static_specs()

    def static_specs(self) -> list[ToolSpec]:
        """:meth:`describe_tools`, synchronously — the routed set is fixed at build."""
        return [self._with_selector(spec) for spec in self._specs.values()]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Resolve the connection, check it may run ``tool``, and run it there."""
        rest = dict(args)
        requested = rest.pop(self._routing.argument, None)
        smuggled = self._smuggled(rest)
        if smuggled:
            return self._deny(tool, requested, "refused_value", smuggled)
        member, reason = self._resolve(requested)
        if member is None:
            return self._deny(tool, requested, "unresolved", reason)
        if not await self._grant_active(member.instance):
            return self._deny(
                tool,
                requested,
                "grant_revoked",
                f"this agent no longer holds the connection {member.instance!r}",
                member=member,
            )
        if tool not in member.approved:
            return self._deny(
                tool,
                requested,
                "unapproved",
                f"{tool} is not approved for the connection {member.instance!r} — an "
                f"operator approves it with Approve on that connection's card",
                member=member,
            )
        if member.read_only and self._classification(tool) != "read_only":
            return self._deny(
                tool,
                requested,
                "read_only",
                f"{member.selector or member.instance} is signed in read-only, so {tool} "
                f"cannot run. An operator can set read_only = no on the connection "
                f"{member.instance!r} and reconnect it to allow this.",
                member=member,
            )
        result = await member.attachment.invoke(tool, rest)
        self._record(_ROUTED_ACTION, "allow", tool, requested, member=member)
        return _named(result, member)

    # --- resolution ------------------------------------------------------------

    def _resolve(self, requested: object) -> tuple[RoutedMember | None, str]:
        """The one member ``requested`` names, or none and the reason."""
        addressable = [member for member in self._members if member.selector]
        if requested is None or requested == "":
            if len(self._members) == 1:
                return self._members[0], ""
            return None, (
                f"this agent holds several {self._extension} connections; pass "
                f"{self._routing.argument} as one of: {self._choices(addressable)}"
            )
        if not isinstance(requested, str):
            return None, f"{self._routing.argument} must be text"
        wanted = self._normalized(requested)
        if wanted is not None:
            for member in addressable:
                if self._normalized(member.selector) == wanted:
                    return member, ""
        return None, (
            f"{self._routing.argument} does not name a connection this agent may use; "
            f"it may use: {self._choices(addressable)}"
        )

    def _normalized(self, value: str) -> str | None:
        """The comparable form, or ``None`` for a value no comparison accepts."""
        if not value or any(
            character.isspace() or unicodedata.category(character).startswith(("C", "Z"))
            for character in value
        ):
            return None
        if self._routing.match == "email":
            return unicodedata.normalize("NFKC", value).casefold()
        return value

    @staticmethod
    def _choices(members: Sequence[RoutedMember]) -> str:
        return ", ".join(member.selector for member in members) or "(none has one set)"

    def _smuggled(self, args: dict[str, Any]) -> str:
        """Why an argument value is refused, or empty when none is."""
        if self._refuse is None:
            return ""
        exempt = set(self._routing.free_text)
        for name, value in args.items():
            if name in exempt or not isinstance(value, str):
                continue
            if self._refuse.search(unicodedata.normalize("NFKC", value)):
                return (
                    f"{name} contains an option or setting this tool never accepts "
                    f"(account, client, home, token or environment); the connection "
                    f"decides those"
                )
        return ""

    def _classification(self, tool: str) -> str:
        spec = self._specs.get(tool)
        return spec.classification if spec is not None else "state_modifying"

    def _with_selector(self, spec: ToolSpec) -> ToolSpec:
        schema = dict(spec.input_schema)
        properties = dict(schema.get("properties", {}))
        properties[self._routing.argument] = {
            "type": "string",
            "description": (
                f"Which of your connected {self._extension} accounts to act as, by its "
                f"{self._routing.field}. Optional when you hold exactly one."
            ),
        }
        schema["properties"] = properties
        schema.setdefault("type", "object")
        return spec.model_copy(update={"input_schema": schema})

    # --- verdicts --------------------------------------------------------------

    def _deny(
        self,
        tool: str,
        requested: object,
        reason: str,
        message: str,
        *,
        member: RoutedMember | None = None,
    ) -> ToolResult:
        self._record(_DENIED_ACTION, "deny", tool, requested, member=member, reason=reason)
        return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content=message)

    def _record(
        self,
        action: str,
        outcome: str,
        tool: str,
        requested: object,
        *,
        member: RoutedMember | None,
        reason: str = "",
    ) -> None:
        """One routing verdict, naming the REAL connection when there is one."""
        emit(
            AuditEvent(
                actor_did=self._agent_did,
                action=action,
                target=f"connector:{member.instance}"
                if member
                else f"connector:{self._extension}",
                outcome=outcome,
                tier=self._tier,
                extra={
                    "extension": self._extension,
                    "tool": tool,
                    "connection": member.instance if member else "",
                    "account": member.selector if member else "",
                    "requested": _recorded(requested),
                    "allowed": len(self._members),
                    "reason": reason,
                },
            ),
            self._sink,
        )


def _recorded(requested: object) -> str:
    """What was asked for, bounded and printable — attacker text, kept as evidence."""
    if requested is None:
        return ""
    text = str(requested)[:_MAX_RECORDED]
    return "".join(
        character if character.isprintable() else f"\\x{ord(character):02x}" for character in text
    )


def _named(result: ToolResult, member: RoutedMember) -> ToolResult:
    """The result, saying which connection and account answered.

    JSON stays JSON: it is wrapped, not prefixed, so a caller that parses the
    answer still can.
    """
    account = member.selector or "(default)"
    if result.outcome is not ToolOutcome.OK:
        content = f"[connection {member.instance}, account {account}] {result.content}"
        return result.model_copy(update={"content": content})
    try:
        body: Any = json.loads(result.content)
    except ValueError:
        body = result.content
    wrapped = json.dumps({"connection": member.instance, "account": account, "result": body})
    return result.model_copy(update={"content": wrapped})


__all__ = ["RoutedAttachment", "RoutedMember"]
