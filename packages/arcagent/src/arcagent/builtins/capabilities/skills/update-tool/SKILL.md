---
name: update-tool
version: 1.0.0
description: Update an existing @tool source file in workspace/.capabilities/, bumping its semver per major/minor/patch.
triggers: [fix the tool, the tool has a bug, change behavior of, bump the version of, update the tool]
tools: [read, update_tool, reload]
---

## Resources

(auto-filled by the loader)

## Contract

Inputs you must have:
- The tool's `name` (matches the file basename in `workspace/.capabilities/`).
- The full `new_source` body — `update_tool` overwrites, it does not patch.
- A clear judgment about which version segment to bump.

Outputs the agent must produce:
- The replaced file with a bumped semver in its `@tool(version=...)` argument.
- A `reload()` call after the update succeeds.

## Knowledge

`update_tool` is a wholesale replacement. There is no partial edit — read the file with `read`, modify the source string in place, then call `update_tool(name, new_source, version_bump)`. The new source MUST contain the bumped version string (e.g. `version="1.2.4"` for a patch bump from `1.2.3`); the validator rejects mismatched versions.

Semver judgment (D-357 — LLM owns this call):

- **patch** — bug fix that doesn't change the function signature, return shape, or success/error contract. The LLM caller's existing prompts continue to work unchanged. Examples: fix a typo in an error message, handle an edge case that previously raised, narrow a regex, fix an off-by-one.
- **minor** — additive change. New optional parameters with safe defaults, new fields in the return string, broader input acceptance. The LLM's existing prompts still work; new prompts can use new functionality.
- **major** — breaking change. Required parameter renamed, return format restructured, behavior of an existing input changed. Any existing LLM prompt may break.

When in doubt, bump higher. A surprised caller is worse than a redundant version bump.

## Steps

1. Read the existing source: `read(file_path="<workspace>/.capabilities/<name>.py")`.
2. Decide the bump level based on the semver rules above. If the change is anything more than a typo fix, prefer minor over patch.
3. Compose the new source — copy the existing body, apply the change, update the `version="..."` in the `@tool(...)` decorator to the bumped value.
4. Call `update_tool(name=..., new_source=..., version_bump="<patch|minor|major>")`. If it returns `Error: must declare version="..."`, the version literal in your source doesn't match the bump — fix and retry.
5. Call `reload()`. The diff should mention `~1 replaced (<name> <old>→<new>)`.
6. Invoke the updated tool with sample arguments to confirm the change works.

## Anti Patterns

- **Don't** call `update_tool` without first reading the existing file. You'll lose context on what already worked.
- **Don't** bump patch when you've changed an argument signature. That's a major.
- **Don't** skip the reload — your update is on disk but the registry still serves the old version.
- **Don't** forget to update the `version="..."` literal inside the source. The validator rejects mismatches.
- **Don't** re-create with `create_tool` to "fix" something. Use `update_tool` so the version trail is preserved.

## Examples

```python
# Bug fix: missing-file path returns a confusing message
old_source = await read(file_path=".capabilities/word_count.py")
new_source = old_source.replace(
    "Error: not a file:",
    "Error: file not found in workspace:",
).replace(
    'version="1.0.0"',
    'version="1.0.1"',
)
await update_tool(name="word_count", new_source=new_source, version_bump="patch")
await reload()
```

## Validation

- `update_tool` returned `Updated tool ... <old>→<new>`.
- `reload()` returned a diff containing `~1 replaced (<name> <old>→<new>)`.
- Live invocation with the previous-failing input now returns the new behavior.
