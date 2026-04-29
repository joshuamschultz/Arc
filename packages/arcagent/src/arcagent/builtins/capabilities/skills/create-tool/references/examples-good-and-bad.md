# Examples — good and bad tool authoring

## Good — single-purpose, read-only

```python
from arcagent.tools._decorator import tool
from arcagent.builtins.capabilities import _runtime

@tool(
    description="Count words in a workspace file",
    classification="read_only",
    capability_tags=["file_read"],
    when_to_use="When you need a quick word count without reading the whole file",
    version="1.0.0",
)
async def word_count(file_path: str) -> str:
    p = _runtime.workspace() / file_path
    if not p.is_file():
        return f"Error: not a file: {file_path}"
    return str(len(p.read_text().split()))
```

Why it's good: one job, classification matches behaviour, error path returns a string (not exception), uses runtime context (no hard-coded paths), no privileged imports.

## Good — wraps existing tools, doesn't reach into internals

```python
@tool(
    description="Find all Python files modified today",
    classification="read_only",
    capability_tags=["file_read"],
    when_to_use="When you want recent Python file changes",
    version="1.0.0",
)
async def todays_python(file_path: str = "") -> str:
    # Composes the existing find tool's logic via the workspace
    # — does not re-implement glob/sort.
    from arcagent.builtins.capabilities.find import find
    return await find(pattern="**/*.py")
```

Why it's good: leverages existing tools, doesn't duplicate logic, docstring matches description.

## Bad — privileged import

```python
import os                                                                # ❌ blocked

@tool(description="list /etc", classification="read_only")
async def list_etc() -> str:
    return "\n".join(os.listdir("/etc"))                                 # ❌ also blocked
```

Why it fails: `import os` is in the AST validator's blocked list. The validator rejects with category `import:os`. Even if you got past the import, `os.listdir` is unavailable in the scrubbed builtins.

## Bad — frame escape

```python
@tool(description="grab parent frame", classification="read_only")
async def parent_frame() -> str:
    import sys
    return repr(sys._getframe().f_back.f_globals)                        # ❌ blocked
```

Why it fails: `f_back` and `f_globals` are blocked attributes. The validator rejects with `attribute:f_back`.

## Bad — wrong classification

```python
@tool(description="overwrite a file", classification="read_only")        # ❌ wrong
async def overwrite(path: str, content: str) -> str:
    (_runtime.workspace() / path).write_text(content)
    return "ok"
```

Why it fails functionally (validator passes): a `read_only` tool may be dispatched concurrently. Two parallel calls overwrite the same file with a race. **Always default to `state_modifying`.**

## Bad — no error handling on the happy path

```python
@tool(description="parse JSON file", classification="read_only", version="1.0.0")
async def parse_json(file_path: str) -> str:
    import json
    return json.dumps(
        json.loads((_runtime.workspace() / file_path).read_text()),       # ⚠️ raises on missing file
        indent=2,
    )
```

Why it's bad: a missing file or invalid JSON raises an exception that bubbles up to the LLM as a stack trace instead of a clean `Error: ...` string. Wrap I/O and parse calls so the LLM can recover.
