"""RunManifest + provenance block (COMP-016) — what makes a number reproducible.

Every result row carries the whole provenance block, redundantly. That is
deliberate: rows from several runs get concatenated, and a row that cannot say
which commit, which resolved config, which dataset bytes and which four models
produced it is not evidence of anything (REQ-210).

The config hash covers the RESOLVED configuration rather than the config files,
because an environment variable can repoint a model id or a provider without
touching a single byte on disk. Only the digest is ever written out — the
resolved config carries env-injected credentials, and an artifact that quotes it
leaks them (LLM07).

The manifest is also where the two declared measurement deviations live in
writing: `workpad` and `policy` are enabled, so the accuracy this harness
reports covers arcmemory plus two further system-prompt summarizers (REQ-200).
Declared, never inferred.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from evaluations.longmemeval.dataset import Dataset

HARNESS_VERSION = "0.1.0"
MANIFEST_FILENAME = "run_manifest.json"
REPO_ROOT = Path(__file__).resolve().parents[2]

MEASUREMENT_SCOPE_NOTE = (
    "The workpad and policy modules were enabled for this run, so the reported "
    "accuracy measures arcmemory together with two additional system-prompt "
    "summarizers, not arcmemory in isolation."
)

QuestionDateSource = Literal["dataset", "derived"]

# An empty model id is the silent-degrade signature: the run still completes and
# the number still looks like a number, with no way to tell which model made it.
ModelId = Annotated[str, StringConstraints(min_length=1)]


class ProvenanceError(Exception):
    """Git state could not be read, so the run cannot be attributed to a commit."""


class GitState(BaseModel):
    """The commit a run was produced from, and whether the tree was modified."""

    sha: str
    dirty: bool


class Provenance(BaseModel):
    """The block stamped into `run_manifest.json` and into every result row.

    `question_id`, `question_date_source` and `session_ingest_order` are the
    per-question facts: they are unset on the run-level block and filled by
    `for_question`, which is what turns the run block into a row block.
    """

    git_sha: str
    git_dirty: bool
    harness_version: str
    config_hash: str
    dataset_sha256: str
    dataset_revision: str
    agent_model_id: ModelId
    judge_model_id: ModelId
    embedder_model_id: ModelId
    distiller_model_id: ModelId
    tier: str
    # ISO-8601 rather than a `datetime`, because a result row is serialized by
    # plain `json.dumps` in the ledger, which cannot encode a datetime.
    run_timestamp_utc: str
    question_id: str | None = None
    question_date_source: QuestionDateSource | None = None
    session_ingest_order: list[str] | None = None

    def for_question(
        self,
        question_id: str,
        *,
        question_date_source: QuestionDateSource,
        session_ingest_order: Sequence[str],
    ) -> Provenance:
        """Return this block stamped with one question's per-row facts."""
        return self.model_copy(
            update={
                "question_id": question_id,
                "question_date_source": question_date_source,
                "session_ingest_order": list(session_ingest_order),
            }
        )


class MeasurementScope(BaseModel):
    """What the reported accuracy actually measures — stated, not left to inference."""

    workpad_enabled: bool
    policy_enabled: bool
    note: str = MEASUREMENT_SCOPE_NOTE

    @model_validator(mode="after")
    def _note_must_state_the_scope(self) -> MeasurementScope:
        if not self.note.strip():
            raise ValueError(
                "measurement_scope.note must state the scope in writing; a blank note "
                "defeats the only field that declares it (REQ-200)."
            )
        declares_both = self.workpad_enabled and self.policy_enabled
        if self.note == MEASUREMENT_SCOPE_NOTE and not declares_both:
            raise ValueError(
                "the default note declares workpad and policy enabled, but this run "
                "disabled at least one of them; write a note that matches the run."
            )
        return self


class RunManifest(BaseModel):
    """One per run: provenance, ingest order, the pre-spend estimate, the scope."""

    provenance: Provenance
    measurement_scope: MeasurementScope
    # question_id -> that question's pinned session order. Rows carry their own
    # copy as well; this is the run-wide record REQ-178 asks for, and without it
    # two runs are not comparable, since the recency channel ranks by ingest order.
    session_ingest_order: dict[str, list[str]] = Field(default_factory=dict)
    # The BudgetGovernor's `Estimate`, dumped. Held structurally loose so COMP-021
    # stays the single owner of that schema.
    dry_run_estimate: dict[str, Any] = Field(default_factory=dict)
    pricing_table_version: str

    def write(self, run_dir: Path) -> Path:
        """Write `run_manifest.json` into `run_dir` and return the path."""
        path = run_dir / MANIFEST_FILENAME
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path


def config_hash(resolved_config: Mapping[str, Any]) -> str:
    """SHA-256 over the canonicalized RESOLVED config.

    Canonical form is compact JSON with every mapping key sorted, so insertion
    order and whitespace cannot move the digest while any changed value always
    does. Lists keep their order — an ingest order is a value here, not
    formatting. Anything JSON cannot express is rendered with `str`, which keeps
    a `Path` or an enum hashable without teaching this function every config type.
    """
    canonical = json.dumps(
        resolved_config,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_git_state(repo_root: Path = REPO_ROOT) -> GitState:
    """Read the real HEAD sha and a real dirty check from git.

    `--porcelain` lists tracked modifications and untracked files but never
    ignored ones, so the run's own gitignored artifacts cannot flip the flag.
    """
    git = shutil.which("git")
    if git is None:
        raise ProvenanceError(
            "git is not on PATH, so the run cannot be attributed to a commit; "
            "provenance without a sha is not provenance."
        )
    return GitState(
        sha=_git(git, ["rev-parse", "HEAD"], repo_root),
        dirty=bool(_git(git, ["status", "--porcelain"], repo_root)),
    )


def _git(git: str, args: list[str], repo_root: Path) -> str:
    result = subprocess.run(  # noqa: S603  # reason: fixed argv, absolute git path, no shell
        [git, *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ProvenanceError(
            f"git {' '.join(args)} failed in {repo_root} (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def build_provenance(
    *,
    resolved_config: Mapping[str, Any],
    dataset: Dataset,
    agent_model_id: str,
    judge_model_id: str,
    embedder_model_id: str,
    distiller_model_id: str,
    tier: str = "personal",
    repo_root: Path = REPO_ROOT,
) -> Provenance:
    """Assemble the run-level provenance block.

    `dataset` is a `Dataset` from `load_dataset`, which has already refused any
    file whose bytes do not hash to the pin (REQ-201) — the digest recorded here
    is that same one, so there is exactly one hash check in the harness.
    """
    git = read_git_state(repo_root)
    return Provenance(
        git_sha=git.sha,
        git_dirty=git.dirty,
        harness_version=HARNESS_VERSION,
        config_hash=config_hash(resolved_config),
        dataset_sha256=dataset.sha256,
        dataset_revision=dataset.revision,
        agent_model_id=agent_model_id,
        judge_model_id=judge_model_id,
        embedder_model_id=embedder_model_id,
        distiller_model_id=distiller_model_id,
        tier=tier,
        run_timestamp_utc=datetime.now(UTC).isoformat(),
    )
