---
name: judge_prompt
description: LLM-as-judge per-dimension scoring prompt for skill evaluation.
tunable: true
---
You are evaluating a skill procedure document on {dimension}.

CALIBRATION:
Score 1 (Poor): Procedure fails on this dimension in most scenarios.
Score 3 (Moderate): Procedure is adequate but has notable gaps.
Score 5 (Excellent): Procedure excels — no issues on this dimension.

{anti_inflation}

CHECKLIST (answer YES or NO for each):
{checklist_text}

EXECUTION TRACE:
Task: {task_summary}
Tool calls:
{tool_calls_text}
Errors: {errors_text}
Outcome: {task_outcome}
Coverage: {coverage_pct:.0f}%

PROCEDURE TO EVALUATE:
{skill_text}

First, evaluate each checklist item with YES/NO and brief reasoning.
Then provide your score (1-5) = count of YES answers.

Respond in JSON:
{{"checklist": [{{"item": str, "answer": bool, "reason": str}}],
"score": int, "rationale": str}}
