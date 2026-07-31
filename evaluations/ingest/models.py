"""Which models the eval agent runs on (COMP-006).

The spec pins the system under test to Claude Sonnet 4.5 and the judge to a
dated GPT-4o, and those pins are the defaults here. They are overridable by
environment variable for one reason: a machine may hold a credential for one
provider and not the other, and a pipeline that cannot be exercised at all is
worse than one exercised on a stated substitute.

An override is never silent. ``cli.resolved_model_ids`` reads the model id back
out of the rendered config rather than re-declaring it, so whatever is set here
lands in the run manifest's provenance block and in every result row. A number
produced under a substitute model is therefore self-describing: the row says
which model answered.

The distiller follows the agent's provider by default. Splitting them would put
consolidation on a provider the run may hold no key for, which fails as a dead
distiller — the silent degradation ``preflight`` exists to catch.
"""

from __future__ import annotations

import os
from typing import Final

_SPEC_AGENT_MODEL: Final = "anthropic/claude-sonnet-4-5-20250929"
"""What SPEC-060 measures. Any other value is a declared substitution."""

AGENT_MODEL: Final = os.environ.get("LME_AGENT_MODEL") or _SPEC_AGENT_MODEL
"""``[llm] model`` — the system under test, as a ``provider/model`` id."""

DISTILL_PROVIDER: Final = AGENT_MODEL.split("/", 1)[0]
"""``[modules.memory.config] distill_provider`` — follows the agent's provider."""

DISTILL_MODEL: Final = AGENT_MODEL.split("/", 1)[-1]
"""``[modules.memory.config] distill_model`` — follows the agent's model."""

IS_SPEC_MODEL: Final = AGENT_MODEL == _SPEC_AGENT_MODEL
"""False when the run is on a substitute, which the manifest has to say."""

__all__ = [
    "AGENT_MODEL",
    "DISTILL_MODEL",
    "DISTILL_PROVIDER",
    "IS_SPEC_MODEL",
]
