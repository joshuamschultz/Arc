"""Built-in ``store_secret`` tool.

Live incident (task #21): a user pasted a Browserbase API token into chat
and the agent wrote it verbatim to a workspace file. Doctrine
(``packages/arcagent/CLAUDE.md``): credentials never touch the filesystem;
vault-backed, short-lived tokens only.

This tool NEVER accepts or persists a secret value — its signature has no
``value``/``secret`` parameter, so there is no argument boundary for a
credential to cross in the first place. It only tells the caller where the
OPERATOR should place the credential, per deployment tier, so the model
can relay clear instructions instead of writing anything itself.

Design gap (reported, not solved here): :class:`~arcagent.core.vault.
protocol.VaultBackend` defines only ``get_secret`` — no write/``set_secret``
path exists on ANY backend (file/env/azure). A true "agent programmatically
provisions a vault secret" flow needs that Protocol extended and every
backend given a write implementation; that is vault-package follow-up
work, deliberately out of scope here (SPEC boundary — see task #21 report).
"""

from __future__ import annotations

from arcagent.builtins.capabilities import _runtime
from arcagent.tools._decorator import tool


def _env_var_name(name: str) -> str:
    return name.upper().replace("-", "_")


@tool(
    name="store_secret",
    description=(
        "Request that a named credential be stored by the operator. Never "
        "accepts the secret value as an argument — returns instructions "
        "for where the operator should place it. Use this instead of "
        "writing an API key, token, or password to any file."
    ),
    classification="read_only",
    capability_tags=["secret_handling"],
    when_to_use=(
        "When the user pastes or provides an API key, token, password, or "
        "other credential that the agent will need again later."
    ),
    version="1.0.0",
)
async def store_secret(name: str) -> str:
    """Return operator guidance for storing ``name``. Never touches disk."""
    if not name.replace("-", "_").isidentifier():
        return f"Error: name {name!r} must be alphanumeric (dashes/underscores allowed)"

    env_var = _env_var_name(name)
    tier = _runtime.tier()

    if tier == "federal":
        return (
            f"Cannot store this credential myself. At federal tier, secrets MUST "
            f"come from the configured vault backend (NIST IA-5) — ask the operator "
            f"to add {name!r} to that vault. I never write credentials to disk or "
            "accept a secret value as a tool argument; relay the request, not the value."
        )

    return (
        f"Cannot store this credential myself — credentials never touch the "
        f"filesystem here (see packages/arcagent/CLAUDE.md). Ask the operator to "
        f"add it to the environment file the agent loads at startup "
        f"(~/.arc/.env, or ~/.arc/arc.env for a systemd deployment — mode 600) as:\n\n"
        f"{env_var}=<the value>\n\n"
        f"then restart the agent. Do not paste the value back into this chat or "
        f"write it to any file — write/edit/create_skill/create_tool/update_skill/"
        f"update_tool all refuse content that looks like a live credential."
    )
