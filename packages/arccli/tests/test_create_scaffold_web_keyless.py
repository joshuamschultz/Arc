"""The scaffolded arcagent.toml must not name a paid web provider as a default.

Every agent `arc agent create` wrote used to carry::

    search_provider = "tavily"
    extract_provider = "firecrawl"

Nobody bought either service. Both tools registered anyway, the model saw them,
called them, and the calls died on a missing API key — turns burned on a
capability the deployment never had. Defaults may only name something that
works on a box with no accounts, so extraction goes through the keyless browser
backend and search ships off.

The parse is `tomllib` over the real rendered template and validation is the
real `WebConfig` (``extra="forbid"``), so a mis-nested or typo'd key fails here
rather than at an agent's first turn.
"""

from __future__ import annotations

import tomllib
from typing import Any

import pytest
from arcagent.core.config import ModuleEntry
from arcagent.modules.web.config import WebConfig

from arccli.commands.agent._common import AGENT_TIERS, render_agent_config

#: Services that bill for access. None of these may be a scaffold default.
_PAID_PROVIDERS = frozenset({"tavily", "firecrawl", "parallel"})


def _web_config(tier: str = "personal") -> dict[str, Any]:
    parsed = tomllib.loads(render_agent_config(name="scaffold-agent", tier=tier))
    entry = ModuleEntry.model_validate(parsed["modules"]["web"])
    config: dict[str, Any] = entry.config
    return config


@pytest.mark.parametrize("tier", AGENT_TIERS)
def test_scaffold_names_no_paid_provider(tier: str) -> None:
    """At every tier, neither provider key may name a service that needs a key."""
    config = _web_config(tier)

    assert config.get("extract_provider") not in _PAID_PROVIDERS
    assert config.get("search_provider") not in _PAID_PROVIDERS


def test_scaffold_default_is_the_keyless_extractor() -> None:
    """Extraction ships working: the browser backend needs no account."""
    assert _web_config()["extract_provider"] == "browser"


def test_scaffold_leaves_search_unset() -> None:
    """No keyless search engine exists, so the scaffold buys nothing on your behalf."""
    assert "search_provider" not in _web_config()


def test_scaffold_web_block_round_trips_through_the_real_config_model() -> None:
    """``extra="forbid"`` — a key the model does not know fails right here."""
    config = WebConfig.model_validate(_web_config())

    assert config.extract_provider == "browser"
    assert config.search_provider is None
