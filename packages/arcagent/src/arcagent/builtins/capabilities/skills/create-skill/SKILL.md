---
name: create-skill
version: 1.0.0
description: Scaffold a new skill folder under workspace/.capabilities/skills/ with frontmatter and the seven required sections.
triggers: [add a skill, write a skill that, document this procedure, capture this workflow as a skill]
tools: [create_skill, write, edit, reload, read]
---

## Resources

(auto-filled by the loader)

## Contract

Inputs you must have:
- A clear, single-sentence statement of what the skill teaches.
- 3-7 trigger phrases — natural-language fragments the LLM might think when this skill applies.
- A list of tools the skill expects to invoke (must already be registered).

Outputs the agent must produce:
- A folder at `workspace/.capabilities/skills/<name>/` with `SKILL.md` plus four sub-folders.
- A `reload()` call after `create_skill` succeeds.

Don't proceed if you can't articulate the trigger phrases. A skill that nothing triggers is dead weight.

## Knowledge

A skill is a 7-section markdown procedure that the LLM reads on demand. Frontmatter spec is in `references/frontmatter-spec.md`. Section-quality rubric (what's a good `## Steps` vs. an anti-pattern) is in `references/section-rubric.md`.

The seven sections are mandatory: `## Resources`, `## Contract`, `## Knowledge`, `## Steps`, `## Anti Patterns`, `## Examples`, `## Validation`. The loader auto-generates `## Resources` from folder contents on every reload — leave it empty, your edits to it will be overwritten.

A skill is **not** a tool. Tools execute; skills teach. If your procedure is a single function call with no decision-making, write a `@tool` instead. Skills are for workflows that benefit from prose context (when to apply, what to avoid, examples).

`tools:` in frontmatter declares dependencies. The validator warns at reload time if a listed tool is not registered (federal-tier blocks; enterprise warns; personal info-only).

## Steps

1. Read this SKILL.md once.
2. Write the description and trigger phrases on paper before scaffolding. If the description doesn't fit in one sentence, the skill is too broad — split it.
3. Call `create_skill(name=..., description=..., triggers=[...], tools=[...])`. The folder appears with empty section bodies.
4. Use `edit` to fill each section in order: `## Contract` → `## Knowledge` → `## Steps` → `## Anti Patterns` → `## Examples` → `## Validation`.
5. Skip `## Resources` — the loader fills it. If you have references, scripts, or templates, add files to those sub-folders.
6. Call `reload()`. The diff should mention `+1 added (<your-skill-name>)`.
7. If the reload emits a `capability:registration_warning` for filler sections, go back to step 4 and replace `N/A` / `none` / empty bodies with real content.

## Anti Patterns

- **Don't** ship a skill with `## Anti Patterns: N/A`. The validator warns; the LLM ignores half-empty skills.
- **Don't** copy a skill from outside the project without rewriting it in the project's voice. A drifting skill teaches the LLM the wrong conventions.
- **Don't** declare tools in `tools:` that you don't actually invoke in `## Steps`. The validator catches the unused declaration eventually.
- **Don't** scope a skill so narrowly it's just a wrapper around one tool call. Use a tool's `when_to_use` field instead.
- **Don't** edit `## Resources` by hand. The loader overwrites it.

## Examples

```python
# Trigger: user asks the agent to "write a runbook for rotating credentials"
await create_skill(
    name="rotate-credentials",
    description="Rotate the vault-stored OAuth token for the messaging integration safely",
    triggers=[
        "rotate credentials",
        "rotate the messaging token",
        "the messaging token is expired",
    ],
    tools=["read", "bash", "write"],
)
# then edit each section, then reload()
```

## Validation

Before declaring the new skill done:
- `create_skill` returned `Created skill ...`.
- `reload()` returned a diff containing `+1 added`.
- The reload did NOT emit `capability:registration_failed` for your skill.
- No `capability:registration_warning` events were emitted (or you've consciously decided to leave a section as filler).
- A read of `<workspace>/.capabilities/skills/<name>/SKILL.md` shows real content in all seven sections.
