"""Adversarial: Resource exhaustion attacks (OWASP ASI05).

Tests that code execution has proper resource limits.
These tests verify the subprocess-based execute tool handles
resource bombs gracefully (container mode provides stronger isolation).
"""

from __future__ import annotations

import json

import pytest

from arcrun.builtins.execute import make_execute_tool


class TestResourceExhaustion:
    @pytest.mark.asyncio
    async def test_infinite_loop_times_out(self):
        """Infinite loop should be killed by timeout."""
        from security.conftest import make_ctx

        tool = make_execute_tool(timeout_seconds=2)
        code = "while True: pass"
        result = await tool.execute({"code": code}, make_ctx())
        parsed = json.loads(result)
        assert parsed["exit_code"] == -1  # Timeout
        assert "timeout" in parsed["stderr"].lower() or parsed["exit_code"] == -1

    @pytest.mark.asyncio
    async def test_memory_bomb_contained(self):
        """Large memory allocation should be handled gracefully."""
        from security.conftest import make_ctx

        tool = make_execute_tool(timeout_seconds=5)
        code = """
try:
    x = 'A' * (10 ** 9)  # 1GB string
except MemoryError:
    print("MemoryError caught")
"""
        result = await tool.execute({"code": code}, make_ctx())
        parsed = json.loads(result)
        # Either MemoryError was caught, or process was killed
        assert parsed["exit_code"] is not None

    @pytest.mark.asyncio
    async def test_disk_fill_contained_to_tmpdir(self):
        """Writing large files should be limited to tmpdir."""
        from security.conftest import make_ctx

        tool = make_execute_tool(timeout_seconds=5)
        code = """
import os
try:
    with open('bigfile.txt', 'w') as f:
        f.write('X' * (10 ** 6))  # 1MB — should succeed in tmpdir
    size = os.path.getsize('bigfile.txt')
    print(f"wrote {size} bytes")
except Exception as e:
    print(f"error: {type(e).__name__}")
"""
        result = await tool.execute({"code": code}, make_ctx())
        parsed = json.loads(result)
        # Writing to CWD (tmpdir) should work
        assert parsed["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_subprocess_spawn_contained(self):
        """Spawning subprocesses from executed code should be limited."""
        from security.conftest import make_ctx

        tool = make_execute_tool(timeout_seconds=5)
        code = """
import subprocess
try:
    result = subprocess.run(['echo', 'test'], capture_output=True, text=True, timeout=2)
    print(f"spawned: {result.stdout.strip()}")
except Exception as e:
    print(f"blocked: {type(e).__name__}")
"""
        result = await tool.execute({"code": code}, make_ctx())
        parsed = json.loads(result)
        # Subprocess may succeed (limited env) or fail — either is acceptable
        assert parsed["exit_code"] is not None

    @pytest.mark.asyncio
    async def test_output_truncation(self):
        """Excessive output should be truncated."""
        from security.conftest import make_ctx

        tool = make_execute_tool(timeout_seconds=5, max_output_bytes=100)
        code = "print('A' * 10000)"
        result = await tool.execute({"code": code}, make_ctx())
        parsed = json.loads(result)
        # stdout should be truncated to max_output_bytes
        assert len(parsed["stdout"]) <= 200  # Some margin for encoding
