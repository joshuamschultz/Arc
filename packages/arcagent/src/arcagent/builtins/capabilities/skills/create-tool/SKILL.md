---
name: create-tool
version: 1.0.0
description: Author a new @tool-decorated Python file in the agent workspace, validate it, and register it via reload().
triggers: [add a tool, build a tool, extend yourself, write a tool that, I need a tool to]
tools: [write, create_tool, reload, read, bash]
---

## Resources

(auto-filled by the loader)

## Contract

Inputs you must have:
- A clear, single-sentence statement of what the new tool does.
- Argument names with concrete types (`str`, `int`, `bool`, `list[str]`).
- A description of the success-case return string.

Outputs the agent must produce:
- A `.py` file at `workspace/.capabilities/<name>.py` decorated with `@tool(...)`.
- A `reload()` call after `create_tool` succeeds.
- A test invocation of the new tool to confirm it works.

Don't proceed if the user's request is ambiguous about WHEN the tool should be called. Ask.

## Knowledge

The `@tool` decorator lives in `arcagent.tools._decorator`. Its full field list is in `references/decorator-fields.md`. The AST validator that `create_tool` runs is documented in `references/ast-blocked-list.md` — read it before writing source that touches files, network, or imports beyond `typing`/`dataclasses`. Good and bad tool authoring patterns are in `references/examples-good-and-bad.md`.

The `version` field is required and must match the bumped version when calling `update_tool` later. Start at `"1.0.0"`.

`classification` is the safety guard: `"read_only"` if the tool only reads state (and is therefore parallel-safe), `"state_modifying"` for anything that writes, calls subprocesses, or touches external state. **Default to `"state_modifying"`** — when unsure, the safer choice loses concurrency, never correctness.

`when_to_use` is the LLM-facing hint shown in the system prompt's tool manifest. Keep it under 80 characters and lead with the trigger phrase ("When you need to ...").

## Steps

1. Read this SKILL.md once. Don't re-read mid-task.
2. Decide the tool's name (snake_case, valid Python identifier) and write a one-line description.
3. Compose the source body — use `templates/tool.py.template` as the starting point.
4. Call `create_tool(name=..., source=...)`. If it returns `Error: AST validation rejected ...`, read the rejection category, fix the offending pattern (it's almost always a privileged import or `getattr` on a denied attribute), and try again.
5. Call `reload()`. The diff string should mention `+1 added (<your-name>)`.
6. Invoke your new tool with sample arguments to confirm it works end-to-end.
7. If anything is wrong, call `update_tool(name=..., new_source=..., version_bump="patch")` — never delete and re-create.

## Anti Patterns

- **Don't** auto-call `reload()` from inside `create_tool`. The split is intentional: write many capabilities, reload once.
- **Don't** put workspace paths in the source body. Use the runtime context (`from arcagent.builtins.capabilities import _runtime; _runtime.workspace()`).
- **Don't** import `os`, `sys`, `subprocess`, `socket`, `ctypes`, `pickle`, `marshal`, `shelve`, or any module not in the AST validator's allowlist. The validator will reject the source and your call to `create_tool` will fail.
- **Don't** name your tool the same as an existing one — `create_tool` rejects collisions. Use `update_tool` instead.
- **Don't** skip the test invocation in step 6. A tool that registers but doesn't work wastes context for every future LLM call.

## Examples

```python
# Good — read-only, single-purpose, classification correct
@tool(
    description="Count lines in a workspace file",
    classification="read_only",
    capability_tags=["file_read"],
    when_to_use="When you need a quick line count without reading the whole file",
    version="1.0.0",
)
async def line_count(file_path: str) -> str:
    from arcagent.builtins.capabilities import _runtime
    from arcagent.tools._validation import resolve_workspace_path
    p = resolve_workspace_path(file_path, _runtime.workspace())
    return str(sum(1 for _ in p.open()))
```

## Validation

Before declaring the new tool done:
- `create_tool` returned `Created tool ...`.
- `reload()` returned a diff containing `+1 added`.
- A live invocation returned the expected output.
- `scripts/validate.py` (if you wrote one alongside the tool) exits 0.
