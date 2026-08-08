"""1Password SDK adapter — the whole third-party side of the onepassword bundle.

The SDK rather than the `op` CLI, because `op` authenticates a session that every
child process inherits: giving an agent the CLI gives it the operator's whole
1Password session, and no manifest allowlist can take that back. Here the
credential is a service-account token held in this process, scoped in 1Password
itself to the one vault this connection may read.

**Every path is read-only, and the vault is not a parameter.** ``vault_id`` comes
from configuration and is never taken from a tool argument, so no argument a model
supplies can widen the blast radius to a second vault. The SDK's write methods
(``create``, ``put``, ``delete``, ``archive``) are not reachable from any verb here.

**Concealed values never ride along.** ``onepassword_get_item`` returns an item's
non-secret fields and the NAMES of its concealed ones. Getting a secret value is
its own verb, taking an explicit ``op://`` reference, so an operator can grant
"see what credentials exist" without granting "read them".

The SDK is imported inside the factory rather than at module scope: a missing
dependency must refuse the install with a sentence naming the package, not blow up
the import of the bundle.
"""

from __future__ import annotations

import json
from typing import Any, Final

from arcagent.extension.attachment import (
    ProbeResult,
    Requirement,
    RequirementKind,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)

#: Reported to 1Password so a vault's access log names what reached it.
_INTEGRATION_NAME: Final = "Arc connector extension"
_INTEGRATION_VERSION: Final = "v1.0.0"

#: The SDK's field type for a value 1Password hides. Named here so the redaction
#: rule is one comparison rather than a scattered string.
_CONCEALED: Final = "Concealed"

_STRING: Final[dict[str, str]] = {"type": "string"}


class OnePasswordAttachment:
    """Reads one 1Password vault through the official SDK, and only reads it."""

    def __init__(self, *, token: str, vault_id: str) -> None:
        self._token = token
        self._vault_id = vault_id
        self._client: Any = None

    # --- the hook contract ---------------------------------------------------

    def requirements(self) -> list[Requirement]:
        """Two credentials and no host prerequisite: the SDK is a declared dependency."""
        return [
            Requirement(
                kind=RequirementKind.CREDENTIAL,
                name="service_account_token",
                instruction="A 1Password service-account token, scoped read-only to one vault",
            ),
            Requirement(
                kind=RequirementKind.CREDENTIAL,
                name="vault_id",
                instruction="The id of the single vault this connection may read",
            ),
        ]

    async def probe(self) -> ProbeResult:
        """List the vault. It is the cheapest call proving the token reaches it."""
        missing = self._missing()
        if missing:
            return ProbeResult(
                reachable=False,
                detail=(
                    f"onepassword has no credential for {', '.join(missing)} — "
                    f"run 'arc connector auth <instance>' to supply them."
                ),
            )
        try:
            overviews = await self._list()
        except Exception as exc:  # reason: any SDK failure is an unreachable vault
            return ProbeResult(reachable=False, detail=f"1Password did not answer: {exc}")
        return ProbeResult(
            reachable=True,
            tools=await self.describe_tools(),
            detail=f"vault {self._vault_id} reachable, {len(overviews)} item(s)",
        )

    async def describe_tools(self) -> list[ToolSpec]:
        """Three read verbs. Classification and tags match extension.toml exactly."""
        return [
            ToolSpec(
                name="onepassword_list_items",
                description=(
                    "List item titles, ids and categories in the connected vault. "
                    "Returns no secret values."
                ),
                input_schema=_schema({}),
                classification="read_only",
                capability_tags=["user_profile"],
            ),
            ToolSpec(
                name="onepassword_get_item",
                description=(
                    "Read one item's non-secret fields. Concealed fields are listed by name "
                    "with their value withheld; use onepassword_resolve_secret for a value."
                ),
                input_schema=_schema({"item_id": _STRING}, required=["item_id"]),
                classification="read_only",
                capability_tags=["user_profile"],
            ),
            ToolSpec(
                name="onepassword_resolve_secret",
                description=(
                    "Resolve one op://vault/item/field reference to its secret value. "
                    "This puts a live credential in the transcript."
                ),
                input_schema=_schema({"reference": _STRING}, required=["reference"]),
                classification="read_only",
                capability_tags=["user_profile"],
            ),
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Run one verb. An SDK failure is an answer the agent reads, not a raise."""
        try:
            return ToolResult(tool=tool, content=await self._dispatch(tool, args))
        except KeyError:
            return _error(tool, f"onepassword has no tool named {tool!r}")
        except ValueError as exc:
            return _error(tool, str(exc))
        except Exception as exc:  # reason: the SDK's error types are not part of our contract
            return _error(tool, f"1Password call failed: {type(exc).__name__}: {exc}")

    # --- verbs ----------------------------------------------------------------

    async def _dispatch(self, tool: str, args: dict[str, Any]) -> str:
        """Route one verb to the SDK. ``KeyError`` means an undeclared name."""
        if tool == "onepassword_list_items":
            return _dump([_overview(item) for item in await self._list()])
        if tool == "onepassword_get_item":
            client = await self._connect()
            item = await client.items.get(self._vault_id, str(args["item_id"]))
            return _dump(_item(item))
        if tool == "onepassword_resolve_secret":
            client = await self._connect()
            return str(await client.secrets.resolve(_reference(args["reference"])))
        raise KeyError(tool)

    async def _list(self) -> list[Any]:
        client = await self._connect()
        return list(await client.items.list(self._vault_id))

    async def _connect(self) -> Any:
        """Authenticate once and reuse. The SDK client is the thing holding the token."""
        if self._client is None:
            # Ignored because the SDK ships no stubs and is an extension-declared
            # dependency, absent from the harness environment by design.
            from onepassword.client import Client  # type: ignore[import-not-found]

            self._client = await Client.authenticate(
                auth=self._token,
                integration_name=_INTEGRATION_NAME,
                integration_version=_INTEGRATION_VERSION,
            )
        return self._client

    def _missing(self) -> list[str]:
        """Which of the two credentials this attachment does not have."""
        held = {"service_account_token": self._token, "vault_id": self._vault_id}
        return sorted(name for name, value in held.items() if not value)


def _overview(item: Any) -> dict[str, Any]:
    """One item as a listing row — identity only, never a field value."""
    return {
        "id": str(item.id),
        "title": str(item.title),
        "category": str(item.category),
        "updated_at": str(item.updated_at),
    }


def _item(item: Any) -> dict[str, Any]:
    """One item with its concealed field VALUES withheld and their names kept."""
    return {
        "id": str(item.id),
        "title": str(item.title),
        "category": str(item.category),
        "tags": [str(tag) for tag in item.tags],
        "fields": [_field(field) for field in item.fields],
    }


def _field(field: Any) -> dict[str, Any]:
    """One field. A concealed value is replaced, not truncated and not hinted at."""
    concealed = str(field.field_type) == _CONCEALED or _CONCEALED in str(field.field_type)
    return {
        "id": str(field.id),
        "title": str(field.title),
        "type": str(field.field_type),
        "value": "[concealed — use onepassword_resolve_secret]" if concealed else str(field.value),
    }


def _reference(value: object) -> str:
    """An op:// secret reference, refused if it is anything else.

    The SDK would reject a malformed reference anyway; refusing here means the
    refusal names the rule instead of surfacing an SDK internal.
    """
    text = str(value)
    if not text.startswith("op://"):
        msg = f"{text!r} is not an op://vault/item/field reference"
        raise ValueError(msg)
    return text


def _schema(
    properties: dict[str, dict[str, str]], *, required: list[str] | None = None
) -> dict[str, Any]:
    """One tool's input schema, closed to anything the verb did not declare."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _dump(body: object) -> str:
    return json.dumps(body, ensure_ascii=False)


def _error(tool: str, content: str) -> ToolResult:
    return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content=content)


def _credential(context: dict[str, Any], key: str) -> str:
    """One declared credential out of the context Arc resolved from its secret store."""
    return str(context.get(key) or "")


def build_native_attachment(context: dict[str, Any]) -> OnePasswordAttachment:
    """The fixed factory Arc calls to build this extension's attachment."""
    return OnePasswordAttachment(
        token=_credential(context, "service_account_token"),
        vault_id=_credential(context, "vault_id"),
    )
