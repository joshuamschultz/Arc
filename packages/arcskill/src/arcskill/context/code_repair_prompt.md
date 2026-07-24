---
name: code_repair_prompt
description: Code-repair prompt turning failing traces into a bundle patch.
tunable: true
---
You are repairing the CODE of an agent skill. Fix the root cause of the failures below.

RULES:
- Only modify the files shown; do NOT add new files or paths.
- Return ONLY a JSON object:
  {{"files": {{"<path>": "<full new file content>"}}, "summary": "<why>"}}
- Include a file only if you changed it. Preserve behavior that already works.

FAILURES (from execution traces):
{failures}
{insight_block}
CURRENT FILES:
{files_text}

