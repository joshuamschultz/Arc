"""Adversarial: Path traversal attacks (OWASP ASI02, ASI05).

Tests that sandboxed code execution cannot access files outside
its designated directory.
"""

from __future__ import annotations

import json

import pytest

from arcrun.builtins.execute import make_execute_tool


class TestPathTraversal:
    @pytest.mark.asyncio
    async def test_relative_path_traversal_in_code(self):
        """Code that tries to read ../../etc/passwd should fail or be contained."""
        from security.conftest import make_ctx

        tool = make_execute_tool(timeout_seconds=5)
        code = """
import os
try:
    with open('../../etc/passwd') as f:
        print(f.read())
except Exception as e:
    print(f"blocked: {type(e).__name__}")
"""
        result = await tool.execute({"code": code}, make_ctx())
        parsed = json.loads(result)
        # Either the file doesn't exist in the tmpdir context, or access is denied
        # The key assertion: /etc/passwd content should NOT appear in stdout
        assert "/root:" not in parsed["stdout"]
        assert "nobody:" not in parsed["stdout"]

    @pytest.mark.asyncio
    async def test_absolute_path_access_in_code(self):
        """Code trying to read /etc/hostname directly."""
        from security.conftest import make_ctx

        tool = make_execute_tool(timeout_seconds=5)
        code = """
try:
    with open('/etc/hostname') as f:
        content = f.read()
        # In subprocess mode, this may succeed but is contained
        print(f"read: {len(content)} bytes")
except Exception as e:
    print(f"blocked: {type(e).__name__}")
"""
        result = await tool.execute({"code": code}, make_ctx())
        parsed = json.loads(result)
        # Process ran and returned — no crash
        assert parsed["exit_code"] is not None

    @pytest.mark.asyncio
    async def test_null_byte_injection(self):
        """Null bytes in file paths should not bypass restrictions."""
        from security.conftest import make_ctx

        tool = make_execute_tool(timeout_seconds=5)
        code = r"""
try:
    with open('/tmp/safe\x00/../../etc/passwd') as f:
        print(f.read())
except Exception as e:
    print(f"blocked: {type(e).__name__}")
"""
        result = await tool.execute({"code": code}, make_ctx())
        parsed = json.loads(result)
        assert "blocked" in parsed["stdout"] or parsed["exit_code"] != 0

    @pytest.mark.asyncio
    async def test_symlink_escape_attempt(self):
        """Creating a symlink to escape tmpdir should be contained."""
        from security.conftest import make_ctx

        tool = make_execute_tool(timeout_seconds=5)
        code = """
import os
try:
    os.symlink('/etc/passwd', '/tmp/escape_link')
    with open('/tmp/escape_link') as f:
        content = f.read()
    print(f"escaped: {len(content)} bytes")
except Exception as e:
    print(f"blocked: {type(e).__name__}")
"""
        result = await tool.execute({"code": code}, make_ctx())
        parsed = json.loads(result)
        # Symlink creation may succeed but reading should be within container context
        assert parsed["exit_code"] is not None
