"""ArtifactScrubber (COMP-015) — the last gate before a row reaches the JSONL.

Every field of a result row that was produced by a model is untrusted text on
the way out: the agent answered from a memory store built out of a third-party
dataset, and the judge answered from the agent's answer. Anything secret-shaped
that entered that chain would otherwise be written verbatim to a file that
outlives the run and gets copied around when a number is disputed (LLM02).

Exception text is on the list for a reason people forget: a provider error body
echoes request context back, so the string that lands in an `error` row can
carry request headers and payload fragments that no other row would contain.

The filter itself is `arcmemory.security.privacy_filter` — the same function the
memory write path uses. It is imported, never reimplemented: a second copy of a
secret-pattern list is a second copy that drifts.
"""

from __future__ import annotations

from typing import Any

from arcmemory.security import privacy_filter

MODEL_DERIVED_PATHS: tuple[tuple[str, ...], ...] = (
    ("answer",),
    ("verdict", "raw_response"),
    ("verdict", "prompt_used"),
    ("error",),
)
"""Dotted paths into a result row whose text a model wrote.

`prompt_used` is on the list because it embeds the agent's answer verbatim, so
scrubbing only `answer` would leave the same text unscrubbed one field over.
Everything absent from this list — `question_id`, `provenance`, `cost`,
`retrieval` — is harness-computed and is deliberately left byte-for-byte intact.
"""


class ArtifactScrubber:
    """Pass every model-derived field of a result row through `privacy_filter`."""

    def scrub_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Filter the model-derived fields of `row` in place and return the same dict.

        Missing fields are not an error: a `void` row has no verdict and an
        `error` row has no answer, and both still have to be written.
        """
        for path in MODEL_DERIVED_PATHS:
            self._filter_at(row, path)
        return row

    def scrub_exception(self, exc: BaseException) -> str:
        """Render `exc` as the scrubbed text an `error` row records.

        The type name is kept because the class is what makes an error row
        triageable; only the message can carry an echoed request body.
        """
        return privacy_filter(f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _filter_at(row: dict[str, Any], path: tuple[str, ...]) -> None:
        parent: dict[str, Any] = row
        for key in path[:-1]:
            child = parent.get(key)
            if not isinstance(child, dict):
                return
            parent = child
        leaf = path[-1]
        value = parent.get(leaf)
        if isinstance(value, str):
            parent[leaf] = privacy_filter(value)


__all__ = ["MODEL_DERIVED_PATHS", "ArtifactScrubber"]
