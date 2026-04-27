"""Extended tests for arcskill.hub.lifecycle — covering uncovered branches.

Targets:
- should_unload: exception handler path (lock file raises)
- start_crl_refresh_task: task creation and cancellation
- _crl_refresh_loop: one iteration (quarantine), CRLUnreachable handling,
  generic exception handling, newly_quarantined logging branch
- _get_crl: cached hit, list-format remote fetch
- _fetch_crl_remote: list format vs dict format
- _quarantine_matching: lock-file load error path
- _quarantine_one: existing revoked_dir removed before move, lock update failure
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest.mock
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest
from arcskill.hub.config import HubConfig, RevocationConfig, TierPolicy
from arcskill.hub.errors import CRLUnreachable
from arcskill.hub.lifecycle import (
    _crl_state,
    _fetch_crl_remote,
    _get_crl,
    _quarantine_matching,
    _quarantine_one,
    should_unload,
    start_crl_refresh_task,
)
from arcskill.lock import HubLockFile, SkillLockEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _federal_config(*, fail_closed: bool = True) -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="federal"),
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json",
            fail_closed_if_unreachable=fail_closed,
            crl_refresh_interval_seconds=3600,
        ),
    )


def _personal_config(*, fail_closed: bool = False) -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="personal"),
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json",
            fail_closed_if_unreachable=fail_closed,
            crl_refresh_interval_seconds=3600,
        ),
    )


def _make_lock(hashes: dict[str, str], tmpdir: Path) -> Path:
    lock = HubLockFile()
    for name, h in hashes.items():
        lock.add_or_update(name, SkillLockEntry(content_hash=h))
    lock_path = tmpdir / "lock.json"
    lock.save(lock_path)
    return lock_path


# ---------------------------------------------------------------------------
# should_unload — exception handler path
# ---------------------------------------------------------------------------


def test_should_unload_returns_false_on_lock_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When lock.is_quarantined raises, should_unload defaults to False (safe)."""
    with patch(
        "arcskill.hub.lifecycle.HubLockFile.load",
        side_effect=RuntimeError("corrupted lock"),
    ):
        result = should_unload("any/skill", lock_path=None)

    assert result is False


# ---------------------------------------------------------------------------
# start_crl_refresh_task — task is created
# ---------------------------------------------------------------------------


def test_start_crl_refresh_task_returns_task() -> None:
    """start_crl_refresh_task returns a running asyncio.Task."""

    async def _run() -> None:
        with patch(
            "arcskill.hub.lifecycle._fetch_crl_remote",
            side_effect=urllib.error.URLError("blocked"),
        ):
            task = await start_crl_refresh_task(
                _personal_config(),
                install_base=None,
                lock_path=None,
            )
            assert isinstance(task, asyncio.Task)
            task.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await task

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# _get_crl — cached hit branch
# ---------------------------------------------------------------------------


def test_get_crl_uses_cache_when_fresh() -> None:
    """_get_crl returns cached hashes without hitting the network."""
    config = _personal_config()
    url = config.revocation.crl_url
    cached_hashes = frozenset(["cached_hash"])
    import time

    _crl_state[url] = (time.monotonic() + 9999, cached_hashes)

    with patch(
        "arcskill.hub.lifecycle._fetch_crl_remote",
        side_effect=AssertionError("should not be called"),
    ):
        result = _get_crl(config)

    assert result == cached_hashes
    _crl_state.pop(url, None)


def test_get_crl_refreshes_when_expired() -> None:
    """_get_crl fetches from remote when cache is stale."""
    config = _personal_config()
    url = config.revocation.crl_url
    import time

    _crl_state[url] = (time.monotonic() - 1.0, frozenset(["stale"]))

    fresh_hashes = frozenset(["fresh_hash"])
    with patch(
        "arcskill.hub.lifecycle._fetch_crl_remote",
        return_value=fresh_hashes,
    ):
        result = _get_crl(config)

    assert result == fresh_hashes
    _crl_state.pop(url, None)


# ---------------------------------------------------------------------------
# _fetch_crl_remote — both JSON schemas
# ---------------------------------------------------------------------------


def test_fetch_crl_remote_list_format() -> None:
    """Legacy flat-list format: ["hash1", "hash2"]."""
    crl_data = json.dumps(["hash1", "hash2"]).encode()
    mock_resp = unittest.mock.MagicMock()
    mock_resp.read.return_value = crl_data
    mock_resp.__enter__ = unittest.mock.MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = unittest.mock.MagicMock(return_value=False)

    with patch("arcskill.hub.lifecycle.urllib.request.urlopen", return_value=mock_resp):
        result = _fetch_crl_remote("https://example.com/crl.json")

    assert result == frozenset(["hash1", "hash2"])


def test_fetch_crl_remote_dict_format() -> None:
    """Preferred dict format: {"revoked": [...]}."""
    crl_data = json.dumps({"revoked": ["hashA", "hashB"]}).encode()
    mock_resp = unittest.mock.MagicMock()
    mock_resp.read.return_value = crl_data
    mock_resp.__enter__ = unittest.mock.MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = unittest.mock.MagicMock(return_value=False)

    with patch("arcskill.hub.lifecycle.urllib.request.urlopen", return_value=mock_resp):
        result = _fetch_crl_remote("https://example.com/crl.json")

    assert "hashA" in result
    assert "hashB" in result


# ---------------------------------------------------------------------------
# _quarantine_matching — lock-file load error
# ---------------------------------------------------------------------------


def test_quarantine_matching_returns_empty_on_lock_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When lock file cannot be loaded, _quarantine_matching returns [] (safe)."""
    config = _federal_config()
    with patch(
        "arcskill.hub.lifecycle.HubLockFile.load",
        side_effect=OSError("lock file missing"),
    ):
        result = _quarantine_matching(
            frozenset(["somehash"]),
            config,
            install_base=None,
            lock_path=None,
        )

    assert result == []


def test_quarantine_matching_skips_already_quarantined() -> None:
    """Skills already marked quarantined are skipped in _quarantine_matching."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        install_base = tmpdir / "skills"
        install_base.mkdir()

        revoked_hash = "badhash" * 8
        lock = HubLockFile()
        # Already quarantined — should not be quarantined again
        lock.add_or_update(
            "already/quarantined",
            SkillLockEntry(content_hash=revoked_hash, quarantined=True),
        )
        lock_path = tmpdir / "lock.json"
        lock.save(lock_path)

        config = _personal_config()
        result = _quarantine_matching(
            frozenset([revoked_hash]),
            config,
            install_base=install_base,
            lock_path=lock_path,
        )

    assert result == []


# ---------------------------------------------------------------------------
# _quarantine_one — existing revoked_dir removal + lock update failure
# ---------------------------------------------------------------------------


def test_quarantine_one_removes_existing_revoked_dir() -> None:
    """If revoked_dir already exists (from prior quarantine), it is removed first."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        base = tmpdir / "skills"
        base.mkdir()

        # Create the skill directory
        safe_name = "test__skill"
        skill_dir = base / safe_name
        skill_dir.mkdir()
        (skill_dir / "skill.py").write_text("# skill\n")

        # Pre-create the revoked directory (simulate prior quarantine)
        revoked_dir = base / "revoked" / safe_name
        revoked_dir.mkdir(parents=True)
        (revoked_dir / "old.py").write_text("# old\n")

        lock = HubLockFile()
        lock.add_or_update("test/skill", SkillLockEntry(content_hash="abc"))
        lock_path = tmpdir / "lock.json"
        lock.save(lock_path)

        result = _quarantine_one("test/skill", base, lock_path)

        # Assertions inside the context so tmpdir is still alive
        assert result is True
        assert not skill_dir.exists()
        assert revoked_dir.exists()
        # The old file should be gone (removed before move)
        assert not (revoked_dir / "old.py").exists()


def test_quarantine_one_returns_false_on_lock_save_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_quarantine_one returns False when lock file save raises."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        base = tmpdir / "skills"
        base.mkdir()

        # No install_dir to move (only testing lock update path)
        with patch(
            "arcskill.hub.lifecycle.HubLockFile.load",
            side_effect=OSError("cannot load"),
        ):
            result = _quarantine_one("no/dir/skill", base, None)

    assert result is False


# ---------------------------------------------------------------------------
# _crl_refresh_loop — error handling paths
# ---------------------------------------------------------------------------


def test_crl_refresh_loop_handles_crl_unreachable() -> None:
    """CRLUnreachable raised in the loop body is caught and logged (loop continues)."""

    async def _run_one() -> None:
        config = _personal_config()
        _crl_state.clear()

        call_count = 0

        async def _fake_sleep(_: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise asyncio.CancelledError

        # Raise CRLUnreachable (not URLError) so the CRLUnreachable handler fires
        with patch(
            "arcskill.hub.lifecycle._fetch_crl_remote",
            side_effect=CRLUnreachable("endpoint unreachable"),
        ):
            with patch("arcskill.hub.lifecycle.asyncio.sleep", side_effect=_fake_sleep):
                from arcskill.hub.lifecycle import _crl_refresh_loop

                with pytest.raises(asyncio.CancelledError):
                    await _crl_refresh_loop(
                        config, install_base=None, lock_path=None
                    )

    asyncio.run(_run_one())


def test_crl_refresh_loop_handles_unexpected_exception() -> None:
    """Unexpected exception in the loop is caught and logged (loop continues)."""

    async def _run_one() -> None:
        config = _personal_config()
        _crl_state.clear()

        call_count = 0

        async def _fake_sleep(_: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise asyncio.CancelledError

        with patch(
            "arcskill.hub.lifecycle._fetch_crl_remote",
            side_effect=ValueError("unexpected"),
        ):
            with patch("arcskill.hub.lifecycle.asyncio.sleep", side_effect=_fake_sleep):
                from arcskill.hub.lifecycle import _crl_refresh_loop

                with pytest.raises(asyncio.CancelledError):
                    await _crl_refresh_loop(
                        config, install_base=None, lock_path=None
                    )

    asyncio.run(_run_one())


def test_crl_refresh_loop_logs_quarantined_skills() -> None:
    """When skills are quarantined, the loop logs the warning message."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        install_base = tmpdir / "skills"
        install_base.mkdir()

        revoked_hash = "revhash" * 8
        safe_name = "bad__pkg"
        skill_dir = install_base / safe_name
        skill_dir.mkdir()

        lock_path = _make_lock({"bad/pkg": revoked_hash}, tmpdir)
        config = _personal_config()

        async def _run_one() -> None:
            call_count = 0

            async def _fake_sleep(_: float) -> None:
                nonlocal call_count
                call_count += 1
                if call_count >= 1:
                    raise asyncio.CancelledError

            with patch(
                "arcskill.hub.lifecycle._fetch_crl_remote",
                return_value=frozenset([revoked_hash]),
            ):
                with patch("arcskill.hub.lifecycle.asyncio.sleep", side_effect=_fake_sleep):
                    from arcskill.hub.lifecycle import _crl_refresh_loop

                    with pytest.raises(asyncio.CancelledError):
                        await _crl_refresh_loop(
                            config,
                            install_base=install_base,
                            lock_path=lock_path,
                        )

        asyncio.run(_run_one())

        lock = HubLockFile.load(lock_path)
        assert lock.is_quarantined("bad/pkg")
