---
name: judge_rubric
description: 'Scoring rubric for the skill-improver judge: per-dimension checklists
  + calibration.'
tunable: true
---
accuracy:
  checklist:
  - All steps lead to correct outcomes
  - No incorrect or misleading instructions
  - Prerequisites are correctly stated
  - Success criteria are verifiable
  - Edge cases are handled correctly
  anti_inflation: A score of 5 requires ZERO factual errors. Most procedures score
    2-4.
efficiency:
  checklist:
  - No redundant or unnecessary steps
  - Steps are in optimal order
  - No unnecessary tool calls implied
  - Procedure achieves goal in minimal steps
  - No repeated information
  anti_inflation: A score of 5 requires every step to be essential. Most procedures
    score 2-4.
error_handling:
  checklist:
  - Common failure modes are anticipated
  - Recovery steps are provided for errors
  - Fallback paths are defined
  - Error messages guide next actions
  - Partial failure scenarios are addressed
  anti_inflation: A score of 5 requires comprehensive error coverage. Most procedures
    score 2-3.
clarity:
  checklist:
  - Each step has a single unambiguous action
  - Technical terms are defined or standard
  - Conditional branches specify both paths
  - Success criteria are explicitly stated
  - A practitioner can execute without interpretation
  anti_inflation: A score of 5 requires ZERO ambiguity. Most procedures score 2-4.
