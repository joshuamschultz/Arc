"""Golden emission: redact-before-write, sign, provenance-tag, pin (H-041)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from arcskill.improver.curation import (
    default_redactor,
    emit_golden_case,
    load_curated_cases,
)
from arcskill.improver.evalgate import load_suite
from arcskill.improver.goldencase import (
    AssertionCheck,
    CuratedGoldenCase,
    PinnedJudgeError,
    rubric_digest,
)


class _RecordingSigner:
    """Writes a real sidecar so the 'signed' guarantee is observable."""

    def __init__(self) -> None:
        self.signed: list[Path] = []

    def sign(self, path: Path, content: bytes) -> None:
        self.signed.append(path)
        path.with_name(path.name + ".arcsig").write_text("sig:" + str(len(content)))


def _skill_dir(tmp_path: Path) -> Path:
    d = tmp_path / "myskill"
    (d).mkdir()
    (d / "SKILL.md").write_text("# myskill\n")
    return d


# --- NAMED TEST 1: a planted secret in a trace body never reaches the golden -----


def test_planted_secret_is_redacted_before_the_golden_is_written(tmp_path: Path) -> None:
    skill_dir = _skill_dir(tmp_path)
    secret = "sk-proj-DEADBEEFdeadbeef0123456789ABCDEF"
    # The operator's ideal is derived from a real trace body that carried a secret.
    case = CuratedGoldenCase(
        case_id="leaky",
        skill_name="myskill",
        gate_type="exact_match",
        source_trace_id="t-1",
        ideal_output=f"the token is {secret} and the answer is 42",
    )
    emitted = emit_golden_case(skill_dir, case, signer=_RecordingSigner())

    # The secret must appear in NO written artifact under evals/.
    for path in (skill_dir / "evals").rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(encoding="utf-8"), path
    # And the reloaded case carries the redacted form, not the secret.
    assert secret not in emitted.case.ideal_output
    assert "[" in emitted.case.ideal_output  # a redaction tag replaced it


def test_emission_signs_both_artifacts(tmp_path: Path) -> None:
    skill_dir = _skill_dir(tmp_path)
    signer = _RecordingSigner()
    case = CuratedGoldenCase(
        case_id="c", skill_name="myskill", gate_type="exact_match", ideal_output="hi"
    )
    emitted = emit_golden_case(skill_dir, case, signer=signer)
    assert emitted.case_path.with_name(emitted.case_path.name + ".arcsig").exists()
    assert emitted.anchor_path.with_name(emitted.anchor_path.name + ".arcsig").exists()
    assert len(signer.signed) == 2


def test_manifest_tags_curated_provenance_and_gate_type(tmp_path: Path) -> None:
    skill_dir = _skill_dir(tmp_path)
    rubric = "Pass iff the output is polite."
    case = CuratedGoldenCase(
        case_id="c", skill_name="myskill", gate_type="judge_rubric", rubric=rubric,
        judge_model_id="anthropic:haiku", rubric_sha256=rubric_digest(rubric),
    )
    emitted = emit_golden_case(skill_dir, case, signer=_RecordingSigner())
    manifest = json.loads((skill_dir / "evals" / ".manifest.json").read_text())
    anchor_rel = emitted.anchor_path.relative_to(skill_dir / "evals").as_posix()
    entry = manifest["files"][anchor_rel]
    assert entry["provenance"] == "curated"
    assert entry["gate_type"] == "judge_rubric"
    assert entry["judge_model_id"] == "anthropic:haiku"
    assert entry["rubric_sha256"] == rubric_digest(rubric)


def test_load_suite_sees_curated_case_as_human_with_gate_type(tmp_path: Path) -> None:
    skill_dir = _skill_dir(tmp_path)
    case = CuratedGoldenCase(
        case_id="c", skill_name="myskill", gate_type="assertions",
        assertions=[AssertionCheck(kind="contains", value="ok")],
    )
    emit_golden_case(skill_dir, case, signer=_RecordingSigner())
    suite = load_suite(skill_dir)
    curated = [c for c in suite if c.curated]
    assert len(curated) == 1
    assert curated[0].gate_type == "assertions"
    assert curated[0].machine_authored is False  # curated counts toward the minimum


def test_judge_rubric_case_without_pin_is_rejected_before_write(tmp_path: Path) -> None:
    skill_dir = _skill_dir(tmp_path)
    case = CuratedGoldenCase(
        case_id="c", skill_name="myskill", gate_type="judge_rubric",
        rubric="r", judge_model_id="", rubric_sha256=rubric_digest("r"),
    )
    with pytest.raises(PinnedJudgeError):
        emit_golden_case(skill_dir, case, signer=_RecordingSigner())
    # Fail-closed: nothing was written.
    assert not (skill_dir / "evals").exists()


def test_load_curated_cases_roundtrips(tmp_path: Path) -> None:
    skill_dir = _skill_dir(tmp_path)
    case = CuratedGoldenCase(
        case_id="c", skill_name="myskill", gate_type="exact_match", ideal_output="v"
    )
    emit_golden_case(skill_dir, case, signer=None)
    loaded = load_curated_cases(skill_dir)
    assert [c.case_id for c in loaded] == ["c"]
    assert loaded[0].gate_type == "exact_match"


def test_default_redactor_scrubs_known_secret_shapes() -> None:
    out = default_redactor("aws AKIAABCDEFGHIJKLMNOP here and sk-abcdefghijklmnopqrstuvwx")
    assert "AKIAABCDEFGHIJKLMNOP" not in out
    assert "sk-abcdefghijklmnopqrstuvwx" not in out
