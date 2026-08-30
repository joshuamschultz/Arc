---
name: merge_prompt
description: Consolidation prompt turning two overlapping skills into one merged skill.
tunable: true
---
You are CONSOLIDATING two agent skills that the Curator has flagged as overlapping or
duplicate. Produce ONE merged skill that keeps every capability either skill provides,
resolves duplication, and reads as a single coherent skill — not two skills stapled
together.

RULES:
- The merged skill MUST preserve every distinct capability/contract item from BOTH
  skills below. Do not drop a behavior just because it looks similar to the other's.
- Do not invent new capabilities the skills don't already have.
- Return ONLY a JSON object:
  {{"files": {{"SKILL.md": "<full merged SKILL.md text>"}}, "summary": "<why merged, what was kept>"}}

SKILL A ({skill_a_name}):
{skill_a_text}

SKILL B ({skill_b_name}):
{skill_b_text}

MERGE RATIONALE (why the Curator flagged these as overlapping):
{reason}
