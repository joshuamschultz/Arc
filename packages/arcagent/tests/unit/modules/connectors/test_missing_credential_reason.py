"""J-U11: a refused attach for a missing credential is a typed reason, never a CLI command.

The card renders ``reason_code`` + ``action`` as a Reconnect button. Text naming a
terminal command reached agents, cards and the audit chain 58 times on the DGX.
"""

from __future__ import annotations

from typing import Any

import pytest

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.manifest import load_manifest
from arcagent.modules.connectors.install import resolve_secrets

_MANIFEST = """
[extension]
name = "acme"
version = "1.0.0"
attachment = "native"

[config.native]
entrypoint = "acme_attachment"

[[secrets]]
name = "api_token"

[tools]
allow = ["ping"]

[[tools.declared]]
name = "ping"
description = "Ping."
classification = "read_only"
"""


class _EmptyStore:
    async def present(self, connection: str) -> set[str]:
        return set()

    async def get(self, ref: Any) -> None:
        return None


async def test_a_missing_credential_refusal_names_a_reason_and_an_action_not_a_command() -> None:
    manifest = load_manifest(_MANIFEST, tier=Tier.PERSONAL)

    with pytest.raises(ExtensionError) as refused:
        await resolve_secrets(
            manifest,
            connection="work",
            store=_EmptyStore(),  # type: ignore[arg-type] # reason: minimal store double
            include_sensitive=False,
        )

    assert refused.value.details["reason_code"] == "credential_missing"
    assert refused.value.details["action"] == "reconnect"
    assert refused.value.details["missing"] == ["api_token"]
    assert "arc connector" not in refused.value.message
