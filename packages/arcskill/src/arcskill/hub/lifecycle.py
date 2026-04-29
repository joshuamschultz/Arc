"""arcskill.hub.lifecycle -- CRL refresh background task and revocation management.

Background task
---------------
``start_crl_refresh_task()`` launches an asyncio background task that:
1. Fetches the CRL at ``config.revocation.crl_url``.
2. Compares the revoked hashes against installed skills in the lock file.
3. For each hit: quarantines the skill (moves to ``revoked/``, marks in lock).
4. Emits a structured audit event so the next agent boot can unload it.

On-boot check
-------------
``check_revocation_on_boot()`` is a synchronous function called once at
agent start.  It reads the last-cached CRL and quarantines any newly
revoked skills.  The module bus unloads quarantined skills on boot via
``should_unload()``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from arcskill.hub.config import HubConfig
from arcskill.hub.errors import CRLUnreachable
from arcskill.lock import HubLockFile

logger = logging.getLogger(__name__)

# In-process CRL state: {url: (expires_at, frozenset[revoked_hashes])}
_crl_state: dict[str, tuple[float, frozenset[str]]] = {}

# Default revoked directory name under install base.
_REVOKED_DIR = "revoked"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_revocation_on_boot(
    config: HubConfig,
    *,
    install_base: Path | None = None,
    lock_path: Path | None = None,
) -> list[str]:
    """Check the CRL on agent start and quarantine any newly revoked skills.

    Called synchronously at agent boot.  Uses cached CRL if fresh enough;
    otherwise attempts a network refresh.

    Parameters
    ----------
    config:
        Hub configuration.
    install_base:
        Override for tests.
    lock_path:
        Override for tests.

    Returns
    -------
    list[str]
        Names of skills that were quarantined during this boot check.
    """
    if not config.enabled:
        return []

    revoked_hashes = _get_crl(config)
    return _quarantine_matching(
        revoked_hashes, config, install_base=install_base, lock_path=lock_path
    )


def should_unload(name: str, lock_path: Path | None = None) -> bool:
    """Return True if *name* is in the lock file and marked quarantined.

    Called by the module bus before loading a skill module so that revoked
    skills are never made available.
    """
    try:
        lock = HubLockFile.load(lock_path)
        return lock.is_quarantined(name)
    except Exception:
        # On any lock-file error, default to not unloading (safe).
        return False


async def start_crl_refresh_task(
    config: HubConfig,
    *,
    install_base: Path | None = None,
    lock_path: Path | None = None,
) -> asyncio.Task[None]:
    """Launch the CRL refresh background task.

    The task runs forever, sleeping between refreshes according to
    ``config.revocation.crl_refresh_interval_seconds``.

    Returns the task object so callers can cancel it on shutdown.
    """
    task = asyncio.create_task(
        _crl_refresh_loop(config, install_base=install_base, lock_path=lock_path),
        name="arcskill.hub.crl_refresh",
    )
    return task


def quarantine_skill(
    name: str,
    config: HubConfig,
    *,
    install_base: Path | None = None,
    lock_path: Path | None = None,
) -> bool:
    """Manually quarantine a skill by moving it to the revoked directory.

    Returns True if the skill was found and quarantined, False if not found.
    This is also the implementation for ``arc skill hub quarantine <name>``.
    """
    if not config.enabled:
        return False

    base = install_base or Path.home() / ".arc" / "skills"
    return _quarantine_one(name, base, lock_path)


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------


async def _crl_refresh_loop(
    config: HubConfig,
    *,
    install_base: Path | None,
    lock_path: Path | None,
) -> None:
    """Periodically refresh the CRL and quarantine newly revoked skills."""
    interval = config.revocation.crl_refresh_interval_seconds
    while True:
        try:
            revoked_hashes = _fetch_crl_remote(config.revocation.crl_url)
            _crl_state[config.revocation.crl_url] = (
                time.monotonic() + interval,
                revoked_hashes,
            )
            newly_quarantined = _quarantine_matching(
                revoked_hashes,
                config,
                install_base=install_base,
                lock_path=lock_path,
            )
            if newly_quarantined:
                logger.warning(
                    "[hub] CRL refresh quarantined %d skill(s): %s",
                    len(newly_quarantined),
                    ", ".join(newly_quarantined),
                )
        except CRLUnreachable as exc:
            logger.error("[hub] CRL refresh failed: %s", exc)
        except Exception as exc:
            logger.error("[hub] Unexpected error in CRL refresh loop: %s", exc)

        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_crl(config: HubConfig) -> frozenset[str]:
    """Return the current revoked-hash set, using cache when fresh."""
    url = config.revocation.crl_url
    now = time.monotonic()
    cached = _crl_state.get(url)
    if cached and now < cached[0]:
        return cached[1]

    try:
        hashes = _fetch_crl_remote(url)
        _crl_state[url] = (
            now + config.revocation.crl_refresh_interval_seconds,
            hashes,
        )
        return hashes
    except (urllib.error.URLError, OSError) as exc:
        if config.revocation.fail_closed_if_unreachable:
            raise CRLUnreachable(f"CRL endpoint {url!r} unreachable at boot: {exc}") from exc
        logger.warning("[hub] CRL unreachable on boot (%s); using empty set", exc)
        return frozenset()


def _fetch_crl_remote(url: str) -> frozenset[str]:
    """Fetch and parse the CRL JSON from *url*."""
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
        data = json.loads(resp.read())
    if isinstance(data, list):
        return frozenset(str(h) for h in data)
    return frozenset(str(h) for h in data.get("revoked", []))


def _quarantine_matching(
    revoked_hashes: frozenset[str],
    config: HubConfig,
    *,
    install_base: Path | None,
    lock_path: Path | None,
) -> list[str]:
    """Check installed skills against *revoked_hashes* and quarantine hits."""
    if not revoked_hashes:
        return []

    base = install_base or Path.home() / ".arc" / "skills"
    try:
        lock = HubLockFile.load(lock_path)
    except Exception as exc:
        logger.error("[hub] Cannot load lock file for CRL check: %s", exc)
        return []

    quarantined: list[str] = []
    for name, entry in lock.skills.items():
        if entry.quarantined:
            continue
        if entry.content_hash in revoked_hashes:
            logger.warning(
                "[hub] Skill %r (hash %s) is in CRL; quarantining",
                name,
                entry.content_hash[:12],
            )
            _quarantine_one(name, base, lock_path)
            quarantined.append(name)

    return quarantined


def _quarantine_one(
    name: str,
    base: Path,
    lock_path: Path | None,
) -> bool:
    """Move the skill directory to ``revoked/`` and update the lock file."""
    safe_name = name.replace("/", "__")
    install_dir = base / safe_name
    revoked_dir = base / _REVOKED_DIR / safe_name

    if install_dir.exists():
        revoked_dir.parent.mkdir(parents=True, exist_ok=True)
        if revoked_dir.exists():
            shutil.rmtree(revoked_dir)
        shutil.move(str(install_dir), str(revoked_dir))
        logger.info("[hub] Moved %s → %s", install_dir, revoked_dir)

    try:
        lock = HubLockFile.load(lock_path)
        updated = lock.quarantine(name)
        if updated:
            lock.save(lock_path)
        return updated
    except Exception as exc:
        logger.error("[hub] Failed to update lock file for quarantine of %r: %s", name, exc)
        return False
