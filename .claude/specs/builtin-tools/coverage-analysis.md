# Coverage Analysis: Built-in Tools (`arcagent/tools/`)

**Date:** 2026-02-14
**Quality Gate:** >= 80% line coverage (CLAUDE.md), >= 90% for core components
**Current Status:** FAILING -- 37.24% total coverage

---

## 1. Coverage Summary

| File | Stmts | Miss | Branch | BrPart | Line Cov | Status |
|------|-------|------|--------|--------|----------|--------|
| `__init__.py` | 9 | 0 | 0 | 0 | **100%** | PASS |
| `_validation.py` | 14 | 10 | 2 | 0 | **25%** | FAIL |
| `bash.py` | 29 | 18 | 8 | 0 | **30%** | FAIL |
| `edit.py` | 26 | 16 | 10 | 0 | **28%** | FAIL |
| `read.py` | 25 | 15 | 8 | 0 | **30%** | FAIL |
| `write.py` | 14 | 4 | 0 | 0 | **71%** | FAIL |
| **TOTAL** | **117** | **63** | **28** | **0** | **37%** | **FAIL** |

**Branch coverage:** 0% (0 of 28 branches covered)

### Why Current Coverage Exists

The only coverage comes from the `__init__.py` import path being exercised by other tests (e.g., `test_agent_integration.py` likely calls `create_builtin_tools`). The `write.py` partial coverage (71%) suggests the `create_tool` function and `RegisteredTool` construction are exercised, but the inner `execute` function (lines 44-49) is never called.

No test file exists at `tests/unit/tools/` -- zero dedicated tool tests.

---

## 2. Critical Gaps (Prioritized by Business Impact)

### P0 -- CRITICAL (Security: Must Fix)

These are security-critical paths in a federal-first codebase. Path traversal and command execution are primary attack vectors (OWASP LLM01, ASI02, ASI05).

#### Gap 1: `_validation.py` -- Path Traversal Prevention (lines 25-42)

**Uncovered:** The entire `resolve_workspace_path` function body.

```python
# Lines 25-42 -- ALL UNCOVERED
workspace = workspace.resolve()
candidate = Path(file_path)
if candidate.is_absolute():         # Branch: absolute path
    resolved = candidate.resolve()
else:                                 # Branch: relative path
    resolved = (workspace / candidate).resolve()
try:
    resolved.relative_to(workspace)
except ValueError as exc:            # Branch: path escapes workspace
    raise ToolError(...)
return resolved
```

**Risk:** This is the ONLY defense against directory traversal attacks (`../../etc/passwd`). If this logic has a bug, every file tool becomes a security vulnerability. Without tests, regressions can silently break the workspace boundary.

**Required tests:**
1. Relative path resolves within workspace
2. Absolute path within workspace passes
3. Absolute path outside workspace raises `ToolError` with code `TOOL_PATH_OUTSIDE_WORKSPACE`
4. `../` traversal attempt raises `ToolError`
5. Deeply nested `../../..` traversal raises `ToolError`
6. Symlink pointing outside workspace raises `ToolError`
7. Path with `..` that resolves back inside workspace passes (e.g., `sub/../file.txt`)

#### Gap 2: `bash.py` -- Command Execution (lines 45-78)

**Uncovered:** The entire `execute` function.

```python
# Lines 45-78 -- ALL UNCOVERED
process = await asyncio.create_subprocess_shell(...)
stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
# Timeout path
process.kill()                         # Branch: timeout
# Output assembly
if stdout: ...                         # Branch: has stdout
if stderr: ...                         # Branch: has stderr
if len(output) > _MAX_OUTPUT_CHARS:    # Branch: truncation
    output = output[:_MAX_OUTPUT_CHARS] + ...
if process.returncode != 0:           # Branch: non-zero exit
    return f"Exit code: ..."
return output if output else "(no output)"  # Branch: empty output
```

**Risk:** Arbitrary command execution is the highest-risk tool. Timeout enforcement, output truncation, and error handling are critical for preventing resource exhaustion (OWASP LLM10) and RCE (ASI05).

**Required tests:**
1. Successful command returns stdout
2. Failed command returns exit code + output
3. Stderr is captured
4. Timeout kills process and returns error message
5. Output > 30,000 chars is truncated with size indicator
6. Empty output returns "(no output)"
7. Command runs in workspace directory (cwd validation)
8. Combined stdout + stderr output

### P1 -- HIGH (Core Functionality)

#### Gap 3: `edit.py` -- String Replacement Logic (lines 58-86)

**Uncovered:** The entire `execute` function.

```python
# Lines 58-86 -- ALL UNCOVERED
if not resolved.exists():              # Branch: file not found
if not resolved.is_file():            # Branch: not a file
if old_string not in content:          # Branch: string not found
count = content.count(old_string)
if not replace_all and count > 1:      # Branch: ambiguous match
if replace_all:                        # Branch: replace all
    new_content = content.replace(old_string, new_string)
else:                                  # Branch: replace first
    new_content = content.replace(old_string, new_string, 1)
```

**Risk:** The uniqueness enforcement (`count > 1` without `replace_all`) is a key safety feature preventing accidental multi-site edits. Untested, it could silently replace wrong locations.

**Required tests:**
1. Single unique match replaced successfully
2. File not found returns error message
3. Not-a-file (directory) returns error message
4. `old_string` not in file returns error message
5. Multiple matches without `replace_all` returns error with count
6. Multiple matches with `replace_all=True` replaces all
7. `replace_all=True` with single match works
8. Empty `new_string` (deletion) works
9. Replacement preserves rest of file content

#### Gap 4: `read.py` -- File Reading with Offset/Limit (lines 53-76)

**Uncovered:** The entire `execute` function.

```python
# Lines 53-76 -- ALL UNCOVERED
if not resolved.exists():              # Branch: file not found
if not resolved.is_file():            # Branch: not a file
text = resolved.read_text(encoding="utf-8")
start = max(0, offset - 1)
if limit > 0:                         # Branch: limit set
    lines = lines[start : start + limit]
else:                                  # Branch: no limit
    lines = lines[start:]
# cat -n formatting
for i, line in enumerate(lines, start=start + 1):
    numbered.append(f"{i:>6}\t{line}")
```

**Risk:** Offset/limit logic is essential for reading large files without context overflow. Line numbering correctness matters for edit tool accuracy (agents reference line numbers).

**Required tests:**
1. Read entire file returns numbered lines
2. File not found returns error message
3. Not-a-file returns error message
4. `offset=5` skips first 4 lines, numbering starts at 5
5. `limit=10` returns exactly 10 lines
6. `offset + limit` combination returns correct slice
7. `offset` beyond file length returns empty
8. Line numbers are right-aligned in cat -n format
9. Empty file returns empty string

#### Gap 5: `write.py` -- File Writing (lines 44-49)

**Uncovered:** The `execute` function body.

```python
# Lines 44-49 -- UNCOVERED
resolved.parent.mkdir(parents=True, exist_ok=True)
resolved.write_text(content, encoding="utf-8")
return f"Written {len(content)} bytes to {file_path}"
```

**Risk:** Parent directory creation (`mkdir parents=True`) is a convenience feature that could mask path issues. The byte count in the return message should be accurate.

**Required tests:**
1. Write to existing file overwrites content
2. Write creates parent directories
3. Write to deeply nested new path creates all parents
4. Return message includes correct byte count
5. Written content is retrievable (round-trip with read)

### P2 -- MEDIUM (Integration)

#### Gap 6: `__init__.py` -- `create_builtin_tools` Integration

**Currently covered (100% line)** but no branch coverage and no explicit tests verifying:
1. Returns exactly 4 tools (read, write, edit, bash)
2. All tools have correct names
3. All tools are `RegisteredTool` instances
4. Tools are scoped to the given workspace

---

## 3. Improvement Plan

### Phase 1: Security-Critical (P0) -- Estimated 45 min

Create `tests/unit/tools/test_validation.py` and `tests/unit/tools/test_bash.py`.

**Expected coverage increase:** +25% (37% -> ~62%)

#### Test File: `tests/unit/tools/test_validation.py`

```python
"""Tests for workspace path validation -- security-critical."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from arcagent.core.errors import ToolError
from arcagent.tools._validation import resolve_workspace_path


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace directory."""
    return tmp_path / "workspace"


@pytest.fixture(autouse=True)
def _setup_workspace(workspace: Path) -> None:
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "nested.txt").write_text("nested")


class TestRelativePaths:
    def test_relative_path_resolves_within_workspace(
        self, workspace: Path
    ) -> None:
        result = resolve_workspace_path("file.txt", workspace)
        assert result == workspace.resolve() / "file.txt"

    def test_nested_relative_path(self, workspace: Path) -> None:
        result = resolve_workspace_path("sub/nested.txt", workspace)
        assert result == workspace.resolve() / "sub" / "nested.txt"

    def test_relative_with_dotdot_resolving_inside(
        self, workspace: Path
    ) -> None:
        result = resolve_workspace_path("sub/../file.txt", workspace)
        assert result == workspace.resolve() / "file.txt"


class TestAbsolutePaths:
    def test_absolute_path_within_workspace(self, workspace: Path) -> None:
        abs_path = str(workspace.resolve() / "file.txt")
        result = resolve_workspace_path(abs_path, workspace)
        assert result == workspace.resolve() / "file.txt"

    def test_absolute_path_outside_workspace_raises(
        self, workspace: Path
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            resolve_workspace_path("/etc/passwd", workspace)
        assert exc_info.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"


class TestTraversalAttacks:
    def test_dotdot_traversal_raises(self, workspace: Path) -> None:
        with pytest.raises(ToolError) as exc_info:
            resolve_workspace_path("../secret.txt", workspace)
        assert exc_info.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"

    def test_deep_traversal_raises(self, workspace: Path) -> None:
        with pytest.raises(ToolError):
            resolve_workspace_path(
                "../../../etc/passwd", workspace
            )

    def test_symlink_escape_raises(self, workspace: Path) -> None:
        link = workspace / "escape_link"
        link.symlink_to("/tmp")
        with pytest.raises(ToolError):
            resolve_workspace_path("escape_link/secret", workspace)
```

#### Test File: `tests/unit/tools/test_bash.py`

```python
"""Tests for bash tool -- command execution, timeouts, truncation."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.tools.bash import create_tool


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture()
def bash_tool(workspace: Path):
    return create_tool(workspace)


class TestBashExecution:
    async def test_successful_command(self, bash_tool) -> None:
        result = await bash_tool.execute(command="echo hello")
        assert "hello" in result

    async def test_failed_command_returns_exit_code(
        self, bash_tool
    ) -> None:
        result = await bash_tool.execute(command="exit 42")
        assert "Exit code: 42" in result

    async def test_stderr_captured(self, bash_tool) -> None:
        result = await bash_tool.execute(
            command="echo error >&2"
        )
        assert "error" in result

    async def test_empty_output(self, bash_tool) -> None:
        result = await bash_tool.execute(command="true")
        assert result == "(no output)"

    async def test_runs_in_workspace_directory(
        self, bash_tool, workspace: Path
    ) -> None:
        result = await bash_tool.execute(command="pwd")
        assert str(workspace.resolve()) in result


class TestBashTimeout:
    async def test_timeout_kills_process(self, bash_tool) -> None:
        result = await bash_tool.execute(
            command="sleep 60", timeout=1
        )
        assert "timed out" in result
        assert "1s" in result


class TestBashOutputTruncation:
    async def test_large_output_truncated(self, bash_tool) -> None:
        # Generate output > 30,000 chars
        result = await bash_tool.execute(
            command="python3 -c \"print('x' * 50000)\""
        )
        assert "truncated" in result
        assert "50000" in result or len(result) <= 30100
```

### Phase 2: Core Functionality (P1) -- Estimated 60 min

Create `tests/unit/tools/test_edit.py`, `tests/unit/tools/test_read.py`, `tests/unit/tools/test_write.py`.

**Expected coverage increase:** +30% (62% -> ~92%)

#### Test File: `tests/unit/tools/test_read.py`

```python
"""Tests for read tool -- file reading with offset/limit."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.tools.read import create_tool


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture()
def read_tool(workspace: Path):
    return create_tool(workspace)


@pytest.fixture()
def sample_file(workspace: Path) -> Path:
    f = workspace / "sample.txt"
    f.write_text("\n".join(f"line {i}" for i in range(1, 21)))
    return f


class TestReadBasic:
    async def test_read_entire_file(
        self, read_tool, sample_file
    ) -> None:
        result = await read_tool.execute(file_path="sample.txt")
        assert "line 1" in result
        assert "line 20" in result

    async def test_line_numbers_present(
        self, read_tool, sample_file
    ) -> None:
        result = await read_tool.execute(file_path="sample.txt")
        # cat -n format: right-aligned number + tab
        assert "     1\t" in result

    async def test_file_not_found(self, read_tool) -> None:
        result = await read_tool.execute(file_path="nope.txt")
        assert "Error" in result
        assert "not found" in result

    async def test_not_a_file(
        self, read_tool, workspace: Path
    ) -> None:
        (workspace / "adir").mkdir()
        result = await read_tool.execute(file_path="adir")
        assert "Error" in result
        assert "Not a file" in result


class TestReadOffsetLimit:
    async def test_offset_skips_lines(
        self, read_tool, sample_file
    ) -> None:
        result = await read_tool.execute(
            file_path="sample.txt", offset=5
        )
        lines = result.strip().split("\n")
        # First returned line should be line 5
        assert "line 5" in lines[0]
        assert "     5\t" in lines[0]

    async def test_limit_restricts_count(
        self, read_tool, sample_file
    ) -> None:
        result = await read_tool.execute(
            file_path="sample.txt", limit=3
        )
        lines = result.strip().split("\n")
        assert len(lines) == 3

    async def test_offset_and_limit_combined(
        self, read_tool, sample_file
    ) -> None:
        result = await read_tool.execute(
            file_path="sample.txt", offset=10, limit=5
        )
        lines = result.strip().split("\n")
        assert len(lines) == 5
        assert "line 10" in lines[0]

    async def test_offset_beyond_file(
        self, read_tool, sample_file
    ) -> None:
        result = await read_tool.execute(
            file_path="sample.txt", offset=999
        )
        assert result == ""

    async def test_empty_file(
        self, read_tool, workspace: Path
    ) -> None:
        (workspace / "empty.txt").write_text("")
        result = await read_tool.execute(file_path="empty.txt")
        assert result == ""
```

#### Test File: `tests/unit/tools/test_edit.py`

```python
"""Tests for edit tool -- string replacement with uniqueness enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.tools.edit import create_tool


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture()
def edit_tool(workspace: Path):
    return create_tool(workspace)


@pytest.fixture()
def target_file(workspace: Path) -> Path:
    f = workspace / "target.txt"
    f.write_text("hello world\nfoo bar\nhello again\n")
    return f


class TestEditBasic:
    async def test_unique_replacement(
        self, edit_tool, target_file
    ) -> None:
        result = await edit_tool.execute(
            file_path="target.txt",
            old_string="foo bar",
            new_string="baz qux",
        )
        assert "Replaced 1" in result
        content = target_file.read_text()
        assert "baz qux" in content
        assert "foo bar" not in content

    async def test_file_not_found(self, edit_tool) -> None:
        result = await edit_tool.execute(
            file_path="missing.txt",
            old_string="x",
            new_string="y",
        )
        assert "Error" in result
        assert "not found" in result

    async def test_not_a_file(
        self, edit_tool, workspace: Path
    ) -> None:
        (workspace / "adir").mkdir()
        result = await edit_tool.execute(
            file_path="adir",
            old_string="x",
            new_string="y",
        )
        assert "Error" in result
        assert "Not a file" in result

    async def test_old_string_not_found(
        self, edit_tool, target_file
    ) -> None:
        result = await edit_tool.execute(
            file_path="target.txt",
            old_string="nonexistent",
            new_string="y",
        )
        assert "Error" in result
        assert "not found" in result


class TestEditUniqueness:
    async def test_multiple_matches_without_replace_all_errors(
        self, edit_tool, target_file
    ) -> None:
        result = await edit_tool.execute(
            file_path="target.txt",
            old_string="hello",
            new_string="goodbye",
        )
        assert "Error" in result
        assert "2 times" in result
        # File should be unchanged
        assert "hello" in target_file.read_text()

    async def test_replace_all_replaces_all_occurrences(
        self, edit_tool, target_file
    ) -> None:
        result = await edit_tool.execute(
            file_path="target.txt",
            old_string="hello",
            new_string="goodbye",
            replace_all=True,
        )
        assert "Replaced 2" in result
        content = target_file.read_text()
        assert "hello" not in content
        assert content.count("goodbye") == 2

    async def test_replace_all_single_match(
        self, edit_tool, target_file
    ) -> None:
        result = await edit_tool.execute(
            file_path="target.txt",
            old_string="foo bar",
            new_string="replaced",
            replace_all=True,
        )
        assert "Replaced 1" in result

    async def test_deletion_via_empty_new_string(
        self, edit_tool, target_file
    ) -> None:
        result = await edit_tool.execute(
            file_path="target.txt",
            old_string="foo bar\n",
            new_string="",
        )
        assert "Replaced 1" in result
        assert "foo bar" not in target_file.read_text()
```

#### Test File: `tests/unit/tools/test_write.py`

```python
"""Tests for write tool -- file writing with parent creation."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.tools.write import create_tool


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture()
def write_tool(workspace: Path):
    return create_tool(workspace)


class TestWriteBasic:
    async def test_write_new_file(
        self, write_tool, workspace: Path
    ) -> None:
        result = await write_tool.execute(
            file_path="new.txt", content="hello"
        )
        assert "Written 5 bytes" in result
        assert (workspace / "new.txt").read_text() == "hello"

    async def test_overwrite_existing_file(
        self, write_tool, workspace: Path
    ) -> None:
        (workspace / "existing.txt").write_text("old")
        result = await write_tool.execute(
            file_path="existing.txt", content="new"
        )
        assert "Written 3 bytes" in result
        assert (workspace / "existing.txt").read_text() == "new"

    async def test_creates_parent_directories(
        self, write_tool, workspace: Path
    ) -> None:
        result = await write_tool.execute(
            file_path="a/b/c/deep.txt", content="deep"
        )
        assert "Written" in result
        assert (workspace / "a" / "b" / "c" / "deep.txt").exists()

    async def test_byte_count_accuracy(
        self, write_tool, workspace: Path
    ) -> None:
        content = "x" * 1234
        result = await write_tool.execute(
            file_path="sized.txt", content=content
        )
        assert "1234 bytes" in result
```

### Phase 3: Integration (P2) -- Estimated 20 min

Create `tests/unit/tools/test_init.py` for `create_builtin_tools`.

**Expected coverage increase:** +3% (92% -> ~95%)

#### Test File: `tests/unit/tools/test_init.py`

```python
"""Tests for create_builtin_tools factory."""

from __future__ import annotations

from pathlib import Path

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.tools import create_builtin_tools


class TestCreateBuiltinTools:
    def test_returns_four_tools(self, tmp_path: Path) -> None:
        tools = create_builtin_tools(tmp_path)
        assert len(tools) == 4

    def test_tool_names(self, tmp_path: Path) -> None:
        tools = create_builtin_tools(tmp_path)
        names = {t.name for t in tools}
        assert names == {"read", "write", "edit", "bash"}

    def test_all_registered_tool_instances(
        self, tmp_path: Path
    ) -> None:
        tools = create_builtin_tools(tmp_path)
        for t in tools:
            assert isinstance(t, RegisteredTool)

    def test_all_native_transport(self, tmp_path: Path) -> None:
        tools = create_builtin_tools(tmp_path)
        for t in tools:
            assert t.transport == ToolTransport.NATIVE
```

---

## 4. Effort Summary

| Phase | Files | Tests | Est. Time | Coverage Target |
|-------|-------|-------|-----------|-----------------|
| Phase 1 (P0 Security) | 2 | ~14 | 45 min | 62% |
| Phase 2 (P1 Core) | 3 | ~22 | 60 min | 92% |
| Phase 3 (P2 Integration) | 1 | ~4 | 20 min | 95% |
| **Total** | **6** | **~40** | **~2 hrs** | **95%** |

---

## 5. Success Criteria

- [ ] Line coverage >= 80% (quality gate pass)
- [ ] Branch coverage >= 75% (quality gate)
- [ ] Core security paths (validation) >= 95%
- [ ] All 8 branch paths in `_validation.py` covered
- [ ] Timeout path in `bash.py` verified with actual timeout
- [ ] Uniqueness enforcement in `edit.py` verified with multi-match scenarios
- [ ] Symlink traversal attack tested
- [ ] Output truncation at 30K chars verified
- [ ] `mypy --strict` passes on all test files
- [ ] `ruff check` passes on all test files

---

## 6. Verdict

**FAILING.** 37.24% total coverage with 0% branch coverage. Zero dedicated tests exist for the tools module. The security-critical path validation (`_validation.py`) is entirely untested -- this is the highest-risk gap given ArcAgent's federal deployment target and OWASP ASI02/ASI03 threat surface.

Phase 1 alone would address the most dangerous gaps. Phases 1+2 together would exceed the 80% quality gate.
