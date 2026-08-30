"""Golden emission — the ONE curation operation (H-041).

Turn a curated case into a **signed, redacted** eval artifact under the skill's
``evals/curated/``, riding the existing suite manifest/provenance. Built ONCE here
(locked design §6): the CLI ``arc skill evals promote`` and the arcui "promote to
golden" surface both call :func:`emit_golden_case` — no duplicated emission logic.

Every emission, in order:

1. **Validate the pin** — a ``judge_rubric`` case without judge id + rubric sha256 is
   rejected BEFORE anything is written (locked design §2).
2. **Redact** — the case's operator-edited text (derived from a real trace body) is a
   raw trace body about to become a *distributable* artifact, so arctrust redaction +
   ``SECRET_PATTERNS`` run over every text field BEFORE the case is written
   (LLM02/LLM07, locked design §4).
3. **Write + sign** — the case JSON and a discoverable pytest anchor land atomically,
   each signed via the injected :class:`~arcskill.improver.seams.Signer` sidecar so the
   hub re-verifies them at load (same signature the whole bundle rides).
4. **Manifest** — provenance-tagged ``curated`` entries with ``gate_type`` and (for
   judge_rubric) the pinned judge id + rubric sha256, add-only beside machine anchors.
5. **Audit** — one ``skill.golden.curated`` event (who curated what, from which trace).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcskill.improver._util import atomic_write_text
from arcskill.improver.goldencase import AssertionCheck, CuratedGoldenCase
from arcskill.improver.seams import Signer

_logger = logging.getLogger("arcskill.improver.curation")

# A redactor turns any text into a redistribution-safe version (PII/secrets removed).
Redactor = Callable[[str], str]

_SIDECAR_SUFFIX = ".arcsig"


@dataclass(frozen=True)
class EmittedGolden:
    """What one emission produced — the paths written and the recorded case."""

    case: CuratedGoldenCase
    case_path: Path
    anchor_path: Path
    nodeid: str


def default_redactor(text: str) -> str:
    """arctrust-backed redactor: regex PII/secret detection + token scrub (LLM02).

    The built-in ``SECRETS`` category (AWS/GitHub/JWT/OpenAI/Anthropic keys, …) is
    on by default, so a structured secret in a trace body is caught; the improver's
    own ``sk-``/``eyJ`` token scrub is layered on as belt-and-suspenders.
    """
    from arctrust import redact_text
    from arctrust.redaction import RegexPiiDetector

    from arcskill.improver._util import _SECRET_TOKEN_RE

    detector = RegexPiiDetector()
    redacted = redact_text(text, detector.detect(text))
    return _SECRET_TOKEN_RE.sub("[REDACTED]", redacted)


def _redact_case(case: CuratedGoldenCase, redactor: Redactor) -> CuratedGoldenCase:
    """Redact every operator-supplied text field before it is written (LLM02)."""
    return replace_case(
        case,
        ideal_output=redactor(case.ideal_output),
        rubric=case.rubric,  # the rubric is operator-authored policy, not trace data
        assertions=[
            AssertionCheck(kind=c.kind, value=redactor(c.value)) for c in case.assertions
        ],
    )


def replace_case(case: CuratedGoldenCase, **changes: Any) -> CuratedGoldenCase:
    """Frozen-model copy-with-changes (pydantic ``model_copy``)."""
    return case.model_copy(update=changes)


def emit_golden_case(
    skill_dir: Path,
    case: CuratedGoldenCase,
    *,
    signer: Signer | None = None,
    redactor: Redactor | None = None,
    audit_sink: Any = None,
    actor_did: str = "",
    tier: str = "personal",
) -> EmittedGolden:
    """Emit ``case`` as a signed, redacted golden under ``skill_dir/evals/curated/``.

    Raises :class:`~arcskill.improver.goldencase.CurationError` (incl. ``PinnedJudgeError``)
    if the case is malformed or unpinned — fail-closed, nothing is written.
    """
    case.validate_pinned()  # judge pin enforced BEFORE any write
    redactor = redactor or default_redactor
    safe = _redact_case(case, redactor)

    curated_dir = skill_dir / "evals" / "curated"
    id8 = _case_id8(safe)
    case_name = f"case_{id8}.json"
    anchor_name = f"test_curated_{id8}.py"
    case_path = curated_dir / case_name
    anchor_path = curated_dir / anchor_name

    case_bytes = (safe.model_dump_json(indent=2) + "\n").encode("utf-8")
    anchor_src = _anchor_source(safe, id8)
    anchor_bytes = anchor_src.encode("utf-8")

    _write_signed(case_path, case_bytes, signer)
    _write_signed(anchor_path, anchor_bytes, signer)
    _record_manifest(skill_dir / "evals", safe, case_name, anchor_name, case_bytes, anchor_bytes)

    nodeid = f"evals/curated/{anchor_name}::test_curated_{id8}"
    _emit_curation_audit(safe, nodeid, audit_sink=audit_sink, actor_did=actor_did, tier=tier)
    _logger.info(
        "curated golden %s emitted for skill %s (gate=%s)", id8, safe.skill_name, safe.gate_type
    )
    return EmittedGolden(case=safe, case_path=case_path, anchor_path=anchor_path, nodeid=nodeid)


def load_curated_cases(skill_dir: Path) -> list[CuratedGoldenCase]:
    """Reload every emitted curated case for ``skill_dir`` (source of truth = the JSON)."""
    curated_dir = skill_dir / "evals" / "curated"
    if not curated_dir.is_dir():
        return []
    cases: list[CuratedGoldenCase] = []
    for path in sorted(curated_dir.glob("case_*.json")):
        try:
            cases.append(CuratedGoldenCase.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            _logger.warning("skipping malformed curated case %s", path)
    return cases


def _case_id8(case: CuratedGoldenCase) -> str:
    """Deterministic 8-hex id from the case identity (stable across re-emits)."""
    key = f"{case.skill_name}:{case.case_id}:{case.gate_type}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def _write_signed(path: Path, content: bytes, signer: Signer | None) -> None:
    """Atomic write + optional sidecar signature (fail-closed re-verify at load)."""
    atomic_write_text(path, content.decode("utf-8"))
    if signer is not None:
        signer.sign(path, content)


def _anchor_source(case: CuratedGoldenCase, id8: str) -> str:
    """A discoverable pytest anchor that verifies the emitted case is well-formed.

    No ``@generated`` marker → :func:`~arcskill.improver.evalgate.load_suite` classifies
    it human-authored (curated cases count toward the gate minimum at every tier). The
    semantic/deterministic per-type comparison itself runs via
    :func:`~arcskill.improver.goldencase.evaluate_curated_case` at improvement time.
    """
    return (
        '"""Curated golden anchor (H-041) — human-curated, gate_type='
        f'{case.gate_type}.\n'
        "\n"
        f"Provenance: curated from trace {case.source_trace_id!r}. Well-formedness and the\n"
        "judge pin (for judge_rubric) are verified here in the sandbox; the semantic/\n"
        "deterministic comparison runs via goldencase.evaluate_curated_case.\n"
        '"""\n'
        "import json\n"
        "from pathlib import Path\n"
        "\n"
        f'CASE = json.loads(Path(__file__).with_name("case_{id8}.json").read_text())\n'
        "\n"
        "\n"
        f"def test_curated_{id8}() -> None:\n"
        '    assert CASE["gate_type"] in {"exact_match", "assertions", "judge_rubric"}\n'
        '    assert CASE["provenance"] == "curated"\n'
        '    if CASE["gate_type"] == "judge_rubric":\n'
        '        assert CASE["judge_model_id"] and CASE["rubric_sha256"]\n'
    )


def _record_manifest(
    evals_dir: Path,
    case: CuratedGoldenCase,
    case_name: str,
    anchor_name: str,
    case_bytes: bytes,
    anchor_bytes: bytes,
) -> None:
    """Add-only, provenance-tagged manifest entries for the curated pair.

    The pytest anchor entry carries ``provenance``/``gate_type`` (+ pinned judge for
    judge_rubric) so :func:`~arcskill.improver.evalgate.load_suite` tags the discovered
    case; the JSON entry records the artifact hash.
    """
    manifest = _read_manifest(evals_dir)
    files = manifest.setdefault("files", {})
    if not isinstance(files, dict):  # pragma: no cover — defensive
        files = manifest["files"] = {}
    entry: dict[str, Any] = {
        "sha256": hashlib.sha256(anchor_bytes).hexdigest(),
        "provenance": "curated",
        "gate_type": case.gate_type,
        "source_trace_id": case.source_trace_id,
    }
    if case.gate_type == "judge_rubric":
        entry["judge_model_id"] = case.judge_model_id
        entry["rubric_sha256"] = case.rubric_sha256
    files[f"curated/{anchor_name}"] = entry
    files[f"curated/{case_name}"] = {
        "sha256": hashlib.sha256(case_bytes).hexdigest(),
        "provenance": "curated",
        "kind": "case-spec",
    }
    atomic_write_text(evals_dir / ".manifest.json", json.dumps(manifest, indent=2))


def _read_manifest(evals_dir: Path) -> dict[str, Any]:
    try:
        raw = json.loads((evals_dir / ".manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _emit_curation_audit(
    case: CuratedGoldenCase,
    nodeid: str,
    *,
    audit_sink: Any,
    actor_did: str,
    tier: str,
) -> None:
    """One operator-signed audit event for the emission (NIST AU-3)."""
    if audit_sink is None:
        return
    from arctrust import AuditEvent, emit

    emit(
        AuditEvent(
            actor_did=actor_did or "did:arc:skill-improver",
            action="skill.golden.curated",
            target=case.skill_name,
            outcome="emitted",
            tier=tier,
            extra={
                "nodeid": nodeid,
                "gate_type": case.gate_type,
                "source_trace_id": case.source_trace_id,
                "judge_model_id": case.judge_model_id,
                "rubric_sha256": case.rubric_sha256,
            },
        ),
        audit_sink,
    )


__all__ = [
    "EmittedGolden",
    "Redactor",
    "default_redactor",
    "emit_golden_case",
    "load_curated_cases",
]
