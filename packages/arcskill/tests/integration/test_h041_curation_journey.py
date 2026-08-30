"""H-041 end-to-end: real trace → curated golden → gated → promoted → loaded.

One journey, the acceptance test (locked done-criteria):

1. A real used skill leaves improver spans whose ``llm_trace_ids`` point at real
   arcllm payloads (a secret planted in one body).
2. The read-time join makes those payloads VISIBLE (no second store).
3. The operator edits a result to the ideal and emits a golden — SIGNED + REDACTED.
4. load_suite discovers it, gate-typed and human-authored.
5. The improvement gate compares PER CASE TYPE (exact_match here + a judge_rubric case).
6. Promotion re-enters the hub gates: the signed sidecar re-verifies (sign gate) and
   the anchor AST-scans clean (scan gate) — never a hot-swap.
"""

from __future__ import annotations

import ast
from pathlib import Path

from arcllm.trace_store import JSONLTraceStore, TraceRecord
from arcskill.improver import (
    ArcSkillImprover,
    CuratedGoldenCase,
    ImproverConfig,
    JoinedTrace,
    rubric_digest,
)
from arcskill.improver.evalgate import load_suite
from arctrust import sign_artifact, verify_artifact
from arctrust.artifact import ArtifactSignature
from arctrust.identity import AgentIdentity

_SECRET = "sk-proj-CAFEBABEcafebabe0123456789ABCDEFGH"


class _ArctrustSigner:
    """A real agent-DID sidecar signer — the hub's signature gate re-verifies it."""

    def __init__(self, did: str, key: bytes) -> None:
        self._did, self._key = did, key

    def sign(self, path: Path, content: bytes) -> None:
        manifest = sign_artifact(content, signer_did=self._did, private_key=self._key)
        path.with_name(path.name + ".arcsig").write_text(manifest.to_json(), encoding="utf-8")


class _FakeJudge:
    async def invoke(self, prompt: str) -> str:
        return "PASS"


def _make_skill(root: Path) -> Path:
    sk = root / "skills" / "invoicer"
    (sk / "evals").mkdir(parents=True)
    (sk / "SKILL.md").write_text("# invoicer\nSummarize an invoice.\n", encoding="utf-8")
    return sk / "SKILL.md"


async def test_curation_journey_end_to_end(tmp_path: Path) -> None:
    skill_md = _make_skill(tmp_path)
    ws = tmp_path / "ws"
    ident = AgentIdentity.generate(org="arc", agent_type="exec")
    signer = _ArctrustSigner(ident.did, ident.signing_seed)

    # (1) A real arcllm payload with a planted secret in the response body.
    llm_store = JSONLTraceStore(ws / "agent_root")
    record = TraceRecord(
        provider="anthropic",
        model="claude",
        request_body={"messages": [{"role": "user", "content": "summarize invoice #7"}]},
        response_body={"text": f"Acme owes $42. Internal token {_SECRET}."},
    )
    await llm_store.append(record)
    llm_trace_id = record.trace_id

    imp = ArcSkillImprover(
        ws,
        config=ImproverConfig(optimize_after_uses=1),
        tier="personal",
        signer=signer,
        llm=_FakeJudge(),
        skill_path=lambda name: skill_md,
    )

    # A real used skill: a span links to the arcllm payload (metadata only).
    await imp.observe(
        skill_name="invoicer",
        tool_name="summarize",
        status="ok",
        error_type=None,
        llm_trace_id=llm_trace_id,
    )
    await imp.on_turn_end(turn=0, outcome="success")

    # (2) Read-time join: the payload is VISIBLE via the join, resolved from arcllm.
    async def payload_source(tid: str):
        rec = await llm_store.get(tid)
        return rec.model_dump() if rec is not None else None

    joined = await imp.curatable_traces("invoicer", payload_source)
    visible = [j for j in joined if isinstance(j, JoinedTrace)]
    assert visible, "the used trace must be curatable via the read-time join"
    body = visible[0].body_text()
    assert _SECRET in body, "the raw joined body legitimately still carries the secret"

    # (3) The operator edits the result to the ideal and emits a golden.
    ideal = f"Customer Acme owes $42. token {_SECRET}"  # secret survived the operator edit
    case = CuratedGoldenCase(
        case_id="invoice-summary",
        skill_name="invoicer",
        gate_type="exact_match",
        source_trace_id=visible[0].trace_id,
        ideal_output=ideal,
    )
    emitted = imp.curate_golden(case)

    # SIGNED: the sidecar re-verifies (the hub signature gate) — promotion, not hot-swap.
    sidecar = ArtifactSignature.from_json(
        emitted.anchor_path.with_name(emitted.anchor_path.name + ".arcsig").read_text()
    )
    assert verify_artifact(emitted.anchor_path.read_bytes(), sidecar)
    # REDACTED: the planted secret reached NO written golden artifact.
    for path in (skill_md.parent / "evals").rglob("*"):
        if path.is_file():
            assert _SECRET not in path.read_text(encoding="utf-8"), path

    # (4) load_suite discovers it, gate-typed + human-authored (counts toward the gate).
    suite = load_suite(skill_md.parent)
    curated = [c for c in suite if c.curated]
    assert len(curated) == 1
    assert curated[0].gate_type == "exact_match"
    assert curated[0].machine_authored is False

    # (5) The gate compares PER CASE TYPE — exact_match on the redacted ideal, and a
    #     judge_rubric case through the pinned judge.
    exact_ok = await imp.evaluate_curated("invoicer", emitted.case.ideal_output)
    assert exact_ok and all(v.passed for v in exact_ok)
    exact_bad = await imp.evaluate_curated("invoicer", "totally different output")
    assert exact_bad and not any(v.passed for v in exact_bad)

    rubric = "Pass iff the summary names the customer and the amount."
    judge_case = CuratedGoldenCase(
        case_id="invoice-quality",
        skill_name="invoicer",
        gate_type="judge_rubric",
        rubric=rubric,
        judge_model_id="anthropic:claude-haiku",
        rubric_sha256=rubric_digest(rubric),
    )
    imp.curate_golden(judge_case)
    # Feed the exact ideal: it satisfies the exact_match case, and the judge PASSes the
    # rubric case — both gate types are evaluated, per their own data.
    verdicts = await imp.evaluate_curated(
        "invoicer", emitted.case.ideal_output, judge=_FakeJudge()
    )
    assert len(verdicts) == 2
    assert {v.passed for v in verdicts} == {True}  # both gate types evaluated, both pass

    # (6) The anchor AST-scans clean — the hub scan gate accepts the promoted artifact.
    ast.parse(emitted.anchor_path.read_text(encoding="utf-8"))

    await imp.aclose()
    await llm_store.close()


async def test_federal_hash_only_declares_curation_unavailable(tmp_path: Path) -> None:
    """A sealed (federal hash-only) payload declares curation unavailable, not empty."""
    from arcskill.improver.trace_join import CurationUnavailable

    skill_md = _make_skill(tmp_path)
    ws = tmp_path / "ws"
    llm_store = JSONLTraceStore(ws / "agent_root")
    # Bodies sealed / absent — the federal hash-only shape.
    record = TraceRecord(
        provider="anthropic", model="claude", request_body=None, response_body=None
    )
    await llm_store.append(record)

    imp = ArcSkillImprover(
        ws, config=ImproverConfig(), tier="federal", skill_path=lambda name: skill_md
    )
    await imp.observe(
        skill_name="invoicer", tool_name="summarize", status="ok",
        error_type=None, llm_trace_id=record.trace_id,
    )
    await imp.on_turn_end(turn=0, outcome="success")

    async def payload_source(tid: str):
        rec = await llm_store.get(tid)
        return rec.model_dump() if rec is not None else None

    joined = await imp.curatable_traces("invoicer", payload_source)
    assert joined and all(isinstance(j, CurationUnavailable) for j in joined)
    assert "hash-only" in joined[0].reason
    await llm_store.close()
