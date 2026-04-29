---
name: update-skill
version: 1.0.0
description: Update an existing skill's SKILL.md body in workspace/.capabilities/skills/, bumping its frontmatter semver.
triggers: [fix the skill, the skill is wrong, refine the procedure, bump the skill version]
tools: [read, update_skill, reload]
---

## Resources

(auto-filled by the loader)

## Contract

Inputs you must have:
- The skill's `name` (matches its folder under `workspace/.capabilities/skills/`).
- The full new body for `SKILL.md` after the frontmatter — `update_skill` rewrites the body wholesale.
- A clear judgment about which version segment to bump.

Outputs the agent must produce:
- The replaced `SKILL.md` with a bumped frontmatter `version`.
- A `reload()` call after the update succeeds.

## Knowledge

`update_skill` preserves the frontmatter (with bumped `version`) and replaces the body wholesale. The body is everything after the closing `---` line; sub-folders (`references/`, `scripts/`, `templates/`, `assets/`) are untouched.

Semver judgment for skills:

- **patch** — wording polish, typo fix, anti-pattern clarification. The skill still teaches the same procedure with the same tools and same outputs.
- **minor** — added an example, added an anti-pattern, refined a step. Existing followers of the skill still produce the same outcome; new followers benefit from the additional context.
- **major** — changed the procedure itself. Different tools listed, different step ordering, different success criteria. An LLM that learned the previous version would now produce wrong output.

A skill that has changed its `tools:` field is always at least a minor bump (caller dependencies changed). If the procedure has changed substantively, it's a major.

## Steps

1. Read the existing SKILL.md: `read(file_path="<workspace>/.capabilities/skills/<name>/SKILL.md")`.
2. Decide the bump level. If you're rewriting more than one section, default to minor; if `## Steps` ordering changed or `tools:` set changed, default to major.
3. Compose the new body (everything after the closing `---`). The frontmatter is regenerated for you — don't include it in `new_body`.
4. Call `update_skill(name=..., new_body=..., version_bump="<patch|minor|major>")`.
5. Call `reload()`. The diff should mention `~1 replaced (<name> <old>→<new>)`.
6. If the validator emits `capability:registration_warning` for any section being filler, go back to step 3 and fill it.

## Anti Patterns

- **Don't** skip the `read` step. Read the existing skill so the new body builds on what was there.
- **Don't** include frontmatter in `new_body`. The tool rejects it implicitly (the next reload sees a doubled `---` block and fails parsing).
- **Don't** bump patch on a major rewrite. Future readers can't tell the skill changed substantively.
- **Don't** delete a section. The 7 sections are mandatory — replace, don't remove.

## Examples

```python
# A new anti-pattern surfaced; add it without changing the procedure.
old = await read(
    file_path=".capabilities/skills/rotate-credentials/SKILL.md"
)
# Strip frontmatter for new_body
new_body = old.split("---\n", 2)[2]
new_body = new_body.replace(
    "## Anti Patterns\n",
    "## Anti Patterns\n\n- **Don't** skip the post-rotation health check.\n",
)
await update_skill(
    name="rotate-credentials", new_body=new_body, version_bump="minor"
)
await reload()
```

## Validation

- `update_skill` returned `Updated skill ... <old>→<new>`.
- `reload()` returned a diff containing `~1 replaced`.
- The reload did NOT emit `capability:registration_failed` for the skill.
- Re-reading the SKILL.md shows the new body content with bumped frontmatter version.
