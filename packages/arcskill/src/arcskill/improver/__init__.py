"""arcskill.improver — evolutionary skill improvement logic (SPEC-044).

The rightful home for skill-improvement mechanics, beside the hub's signed
install/verify/lock lifecycle. Pure logic over injected Protocol seams
(``Mutator``/``Judge``/``EvalRunner``/``Signer``/``AuditSink``): this subpackage
imports no ``arcagent``/``arcllm``/``arcmemory`` (REQ-004, D-3). arcagent wires
the seams through ``arcagent.skilladapt`` and drives it via the ``SkillAdapter``
Protocol.
"""

from arcskill.improver.config import ImproverConfig
from arcskill.improver.curation import (
    EmittedGolden,
    default_redactor,
    emit_golden_case,
    load_curated_cases,
)
from arcskill.improver.goldencase import (
    AssertionCheck,
    CaseVerdict,
    CuratedGoldenCase,
    CurationError,
    PinnedJudgeError,
    evaluate_curated_case,
    rubric_digest,
)
from arcskill.improver.improver import ArcSkillImprover
from arcskill.improver.trace_join import (
    CurationUnavailable,
    JoinedTrace,
    TraceJoin,
)

__all__ = [
    "ArcSkillImprover",
    "AssertionCheck",
    "CaseVerdict",
    "CuratedGoldenCase",
    "CurationError",
    "CurationUnavailable",
    "EmittedGolden",
    "ImproverConfig",
    "JoinedTrace",
    "PinnedJudgeError",
    "TraceJoin",
    "default_redactor",
    "emit_golden_case",
    "evaluate_curated_case",
    "load_curated_cases",
    "rubric_digest",
]
