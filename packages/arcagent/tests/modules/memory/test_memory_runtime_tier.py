"""SPEC-083 — the memory brain always runs at the agent's real deployment tier.

``configure(tier=...)`` receives the agent's runtime tier (``[security].tier``).
A ``[modules.memory.config]`` block that omits ``tier`` must still build the
arcmemory brain with that tier's stringency settings — a federal agent never gets
personal-tier memory. A block ``tier`` that disagrees with the agent tier is
refused at configure (fail closed), never silently used.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arctrust.identity import AgentIdentity

from arcagent.modules.memory import _runtime

pytest.importorskip("arcmemory")

from arcmemory import MemoryConfig as ArcMemoryConfig


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    _runtime.reset()
    yield
    _runtime.reset()


def _configure(tmp_path: Path, *, config: dict[str, Any], tier: str) -> str:
    identity = AgentIdentity.generate(org="default", agent_type="executor")
    _runtime.configure(
        config={"brain": "arcmemory", "backend": {"embed_backend": "none"}, **config},
        workspace=tmp_path,
        agent_did=identity.did,
        identity=identity,
        tier=tier,
    )
    return identity.did


def _brain_config(did: str) -> ArcMemoryConfig:
    brain = _runtime.state_for(did).brain
    assert type(brain).__name__ == "ArcMemoryBrain"
    # The built brain's own settings — the only place the tier's stringency lands.
    config: ArcMemoryConfig = brain._cfg  # type: ignore[attr-defined]  # reason: asserting the built brain's private settings
    return config


def test_federal_agent_without_memory_tier_builds_a_federal_brain(tmp_path: Path) -> None:
    federal = ArcMemoryConfig.for_tier("federal")
    personal = ArcMemoryConfig.for_tier("personal")
    assert federal.datastore_sample_values != personal.datastore_sample_values

    did = _configure(tmp_path, config={}, tier="federal")

    built = _brain_config(did)
    assert built.tier == "federal"
    assert built.datastore_sample_values == federal.datastore_sample_values
    assert built.ingest_max_batch == federal.ingest_max_batch
    assert _runtime.state_for(did).config.tier == "federal"


def test_agent_tier_is_normalized_before_the_brain_is_built(tmp_path: Path) -> None:
    did = _configure(tmp_path, config={}, tier=" Federal ")

    assert _brain_config(did).tier == "federal"


def test_memory_tier_matching_the_agent_tier_is_accepted(tmp_path: Path) -> None:
    did = _configure(tmp_path, config={"tier": "Enterprise"}, tier="enterprise")

    assert _brain_config(did).tier == "enterprise"


@pytest.mark.parametrize(
    ("memory_tier", "agent_tier"), [("personal", "federal"), ("federal", "personal")]
)
def test_memory_tier_disagreeing_with_the_agent_tier_is_refused(
    tmp_path: Path, memory_tier: str, agent_tier: str
) -> None:
    with pytest.raises(ValueError, match="tier"):
        _configure(tmp_path, config={"tier": memory_tier}, tier=agent_tier)

    with pytest.raises(RuntimeError):
        _runtime.state()


def test_without_an_agent_tier_the_memory_block_tier_still_applies(tmp_path: Path) -> None:
    did = _configure(tmp_path, config={"tier": "enterprise"}, tier="")

    assert _brain_config(did).tier == "enterprise"
