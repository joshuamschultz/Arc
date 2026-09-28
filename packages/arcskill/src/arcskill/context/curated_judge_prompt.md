---
name: curated_judge_prompt
description: Strict PASS/FAIL judge for a curated judge_rubric golden case (pinned rubric + candidate output).
tunable: true
---
You are a strict evaluator. Apply the rubric to the candidate output.
Answer with exactly one word on the first line: PASS or FAIL.

RUBRIC:
{rubric}

CANDIDATE OUTPUT:
{candidate_output}
