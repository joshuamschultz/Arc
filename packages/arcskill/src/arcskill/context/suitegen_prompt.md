---
name: suitegen_prompt
description: Golden-suite generation prompt for candidate pytest cases.
tunable: true
---
Generate pytest golden regression cases for the skill '{skill_name}'.
Ground every assertion oracle ONLY in the declared Contract, Examples, and
Validation sections below — never in observed behavior, so current bugs are
not frozen in as expectations.

{skill_text}

Return one Python module containing at most {max_cases}
top-level `def test_*` functions and nothing else.
