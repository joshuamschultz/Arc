---
name: reflection_prompt
description: Constrained-mutation reflection prompt for the skill reflector.
tunable: true
---
You are improving a skill procedure document.

RULES:
- DO NOT modify the SKILL INTENT [IMMUTABLE] section
- Focus your revision ONLY on: {dims_text}
- DO NOT add unnecessary caveats or hedging language
- The revised skill must be under {token_budget} tokens
- Produce specific, actionable steps — not descriptions

CURRENT SKILL:
{current_text}

FAILURE PATTERNS (across execution traces):
{patterns_text}

WEAKEST DIMENSIONS:
{dims_text}

Identify the root cause pattern across these failures.
Then produce an improved version of the skill inside ```markdown``` fences.
