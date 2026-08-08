"""SPEC-064 T-002 — ``EnvFile``, the one owner-only env-file recipe.

``LocalFileSecretBackend`` had this recipe inline: 0600 from creation, an owner +
mode check before every read, ``O_NOFOLLOW``, a private temp file, ``fsync``, and
``os.replace``. The provider-key store needs exactly the same guarantees over
``~/.arc/.env``, and a second implementation of a credential file is a second
place to get a permission bit wrong (D-582). These tests hold the primitive to the
properties the backend was already trusted for — the backend's own suite in
``test_secrets.py`` proves nothing regressed above it.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from arcagent.core.errors import ExtensionError
from arcagent.extension.secrets import EnvFile


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".env"


async def test_a_missing_file_reads_as_empty(env_path: Path) -> None:
    """A store that was never written is empty, not an error — first run is normal."""
    assert await EnvFile(env_path).read() == {}


async def test_a_written_entry_reads_back(env_path: Path) -> None:
    env_file = EnvFile(env_path)
    await env_file.put("ANTHROPIC_API_KEY", "sk-ant-value")
    assert await env_file.read() == {"ANTHROPIC_API_KEY": "sk-ant-value"}


async def test_a_second_entry_preserves_the_first(env_path: Path) -> None:
    env_file = EnvFile(env_path)
    await env_file.put("ANTHROPIC_API_KEY", "sk-ant-value")
    await env_file.put("OPENAI_API_KEY", "sk-openai-value")
    assert await env_file.read() == {
        "ANTHROPIC_API_KEY": "sk-ant-value",
        "OPENAI_API_KEY": "sk-openai-value",
    }


async def test_delete_reports_whether_there_was_anything_to_remove(env_path: Path) -> None:
    env_file = EnvFile(env_path)
    await env_file.put("GROQ_API_KEY", "gsk-value")
    assert await env_file.delete("GROQ_API_KEY") is True
    assert await env_file.delete("GROQ_API_KEY") is False
    assert await env_file.read() == {}


async def test_the_file_is_owner_only_from_creation(env_path: Path) -> None:
    await EnvFile(env_path).put("ANTHROPIC_API_KEY", "sk-ant-value")
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_path.parent.stat().st_mode) == 0o700


async def test_a_loosened_file_is_refused_rather_than_read(env_path: Path) -> None:
    """A world-readable credential file is a finding, not something to quietly parse."""
    env_file = EnvFile(env_path)
    await env_file.put("ANTHROPIC_API_KEY", "sk-ant-value")
    env_path.chmod(0o644)

    with pytest.raises(ExtensionError) as excinfo:
        await env_file.read()
    assert excinfo.value.code == "SECRET_STORE_LOOSE_PERMS"


async def test_an_interrupted_write_leaves_the_previous_file(
    env_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = EnvFile(env_path)
    await env_file.put("ANTHROPIC_API_KEY", "sk-ant-value")
    before = env_path.read_bytes()

    def boom(src: object, dst: object) -> None:
        raise OSError("interrupted before the rename landed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="interrupted"):
        await env_file.put("OPENAI_API_KEY", "sk-openai-value")

    assert env_path.read_bytes() == before
    assert list(env_path.parent.glob("*.tmp*")) == [], "a partial write was left behind"


async def test_concurrent_writes_do_not_lose_each_other(env_path: Path) -> None:
    """Two writers forced to the same instant both land (read-modify-write hazard).

    [[feedback_concurrency_tests_must_interleave]] — the barrier is what makes the
    two coroutines reach their write together; without it ``gather`` runs them one
    after the other and a lost update never fires.
    """
    env_file = EnvFile(env_path)
    barrier = asyncio.Barrier(2)

    async def write(key: str, value: str) -> None:
        await barrier.wait()
        await env_file.put(key, value)

    await asyncio.gather(
        write("ANTHROPIC_API_KEY", "sk-ant-value"),
        write("OPENAI_API_KEY", "sk-openai-value"),
    )

    assert await env_file.read() == {
        "ANTHROPIC_API_KEY": "sk-ant-value",
        "OPENAI_API_KEY": "sk-openai-value",
    }
