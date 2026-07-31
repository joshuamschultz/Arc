"""The memory limits the eval agent is configured with (COMP-006).

Three numbers decide what this benchmark is *able* to measure, and each is read
by more than one component — the emitted TOML, the chunker and fidelity gate
that pack beneath the cap, the dry-run estimator, the two corpus report scripts.
They live here, above every one of those readers, so a raise lands everywhere at
once instead of in the three places somebody remembered.

``evaluations/longmemeval`` imports these; ``evaluations/longmemeval/ingest`` owns them, so
the dependency still points one way. ``longmemeval/preflight.py`` deliberately
does NOT import them — an assumption guard that reads its expectations out of
the thing it guards agrees with any drift it was written to catch.
"""

from __future__ import annotations

from typing import Final

MAX_EVENT_CHARS: Final = 6000
"""``[modules.memory.config.dynamics] max_event_chars`` — arcmemory's sanitize cap.

``arcmemory.capture`` calls ``sanitize(text, max_length=<this>)`` and discards
the return value's length, so a chunk above the cap is silently shortened rather
than refused. The chunker therefore refuses first (REQ-177), which makes this
number the ceiling on the corpus the benchmark can ingest at all.

Measured against the 500-question oracle corpus, the longest single turn is
5,425 characters and 31.75% of all 10,960 turns exceed arcmemory's own 2,000
default — at that default 867 of 948 sessions are refused and 445 of 500
questions void. 6,000 clears the longest turn with room for the 27-character
date prefix, its newline, and the growth NFKC normalization can add before
``sanitize`` measures the text.
"""

RECALL_TOP_K: Final = 20
"""``[modules.memory.config] top_k`` — recall count at ``assemble_prompt``."""

RECALL_BUDGET: Final = 34_000
"""``[modules.memory.config] budget`` — the recall bundle's ceiling.

Sized as ``RECALL_TOP_K`` chunks of the chunker's 1,700-character packing target
so that ``top_k`` is what bounds recall and means what it says. Deliberately
generous: ``enforce_budget`` spends this in *estimated tokens* (~4 chars each),
so the true cost of 20 full chunks is around a quarter of it — over-provisioning
is what keeps the budget from silently becoming the binding constraint, which is
exactly what the 1,024 default did (one recall survived, whatever ``top_k`` said).
"""

__all__ = ["MAX_EVENT_CHARS", "RECALL_BUDGET", "RECALL_TOP_K"]
