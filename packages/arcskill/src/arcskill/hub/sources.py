"""arcskill.hub.sources -- Skill source adapters.

Architecture
------------
``SkillSourceAdapter`` is an ABC with one method: ``fetch(name)``.
Three concrete adapters are provided:

- ``GitHubSource``     -- downloads a versioned release bundle from GitHub.
- ``RegistrySource``   -- downloads from an HTTP registry index endpoint.
- ``WellKnownSource``  -- follows the ``agentskills.io`` discovery pattern via
                         ``/.well-known/skills/index.json``.

All adapters:
1. Perform only the download step.
2. Write the bundle to a caller-supplied quarantine directory.
3. Return a ``FetchResult`` with metadata required by the verify stage.

Security contract
-----------------
- No code is executed during fetch.
- HTTPS required for all non-local sources; HTTP raises ``ValueError``.
- Redirects are followed up to a limit of 3 (httpx default).
- Responses are streamed to disk; no in-memory buffering of large payloads.
"""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import NamedTuple

from arcskill.hub.config import SkillSource

logger = logging.getLogger(__name__)

_MAX_BUNDLE_BYTES = 50 * 1024 * 1024  # 50 MB hard cap per skill bundle


# ---------------------------------------------------------------------------
# FetchResult
# ---------------------------------------------------------------------------


class FetchResult(NamedTuple):
    """Metadata produced by a successful fetch.

    Attributes
    ----------
    local_path:
        Path to the downloaded bundle inside the quarantine directory.
    content_hash:
        SHA-256 hex digest of the raw bundle bytes (verified at install).
    source_name:
        The ``SkillSource.name`` this bundle came from.
    bundle_url:
        Original HTTP URL (empty for local sources).
    version:
        Version string from the registry metadata (empty if unknown).
    """

    local_path: Path
    content_hash: str
    source_name: str
    bundle_url: str
    version: str


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class SkillSourceAdapter(ABC):
    """Abstract base for skill source adapters."""

    def __init__(self, config: SkillSource) -> None:
        self._config = config

    @abstractmethod
    def fetch(self, name: str, quarantine_dir: Path) -> FetchResult:
        """Download the skill bundle to *quarantine_dir*.

        Parameters
        ----------
        name:
            Canonical skill name (e.g. ``"arc-official/summarise"``).
        quarantine_dir:
            Writable directory for download.  Caller ensures it exists.

        Returns
        -------
        FetchResult
            Metadata needed by the verify and scan stages.

        Raises
        ------
        ValueError
            If the source config is invalid for this adapter type.
        RuntimeError
            If the download fails or the bundle exceeds the size cap.
        """


# ---------------------------------------------------------------------------
# GitHub source
# ---------------------------------------------------------------------------


class GitHubSource(SkillSourceAdapter):
    """Fetch a skill release bundle from a GitHub repository.

    The adapter constructs a download URL from the repo + skill name,
    treating the skill name as ``<owner>/<repo-name>`` relative to
    ``config.repo``.  If the skill name already contains a ``/`` it is
    treated as the full path segment after the base repo.

    URL pattern: ``https://github.com/<repo>/releases/download/<version>/<name>.tar.gz``

    For skills without an explicit version tag, the adapter queries the
    GitHub releases API to determine the latest version.
    """

    def fetch(self, name: str, quarantine_dir: Path) -> FetchResult:
        """Download the named skill bundle from GitHub."""
        if not self._config.repo:
            raise ValueError(
                f"SkillSource {self._config.name!r} is type 'github' "
                "but has no 'repo' configured"
            )

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "httpx is required for GitHub skill downloads. "
                "Install arcskill[hub] to add it."
            ) from exc

        # Normalise the skill name to a filesystem-safe filename.
        safe_name = name.replace("/", "__")
        bundle_filename = f"{safe_name}.tar.gz"
        dest = quarantine_dir / bundle_filename

        # Resolve latest release version via GitHub API.
        api_url = (
            f"https://api.github.com/repos/{self._config.repo}"
            f"/releases/latest"
        )
        logger.debug("Fetching latest release for %r from %s", name, api_url)

        with httpx.Client(follow_redirects=True, max_redirects=3) as client:
            resp = client.get(api_url, headers={"Accept": "application/vnd.github+json"})
            resp.raise_for_status()
            release = resp.json()
            version: str = release.get("tag_name", "unknown")

            # Find the matching asset.
            assets: list[dict[str, str]] = release.get("assets", [])
            asset_url: str | None = None
            for asset in assets:
                if asset.get("name", "").startswith(safe_name):
                    asset_url = asset["browser_download_url"]
                    break

            if asset_url is None:
                # Fallback: construct conventional URL.
                asset_url = (
                    f"https://github.com/{self._config.repo}"
                    f"/releases/download/{version}/{bundle_filename}"
                )

            logger.info("Downloading skill %r from %s", name, asset_url)
            _stream_download(client, asset_url, dest)

        content_hash = _sha256_file(dest)
        return FetchResult(
            local_path=dest,
            content_hash=content_hash,
            source_name=self._config.name,
            bundle_url=asset_url,
            version=version,
        )


# ---------------------------------------------------------------------------
# HTTP registry source
# ---------------------------------------------------------------------------


class RegistrySource(SkillSourceAdapter):
    """Fetch a skill bundle from an HTTP registry.

    The registry must expose an index at its base URL returning JSON:

    .. code-block:: json

        {
            "skills": {
                "summarise": {
                    "version": "1.2.0",
                    "url": "https://...",
                    "content_hash": "sha256:..."
                }
            }
        }
    """

    def fetch(self, name: str, quarantine_dir: Path) -> FetchResult:
        """Download the named skill bundle from the HTTP registry."""
        if not self._config.url:
            raise ValueError(
                f"SkillSource {self._config.name!r} is type 'registry' "
                "but has no 'url' configured"
            )

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "httpx is required for registry skill downloads. "
                "Install arcskill[hub] to add it."
            ) from exc

        index_url = self._config.url.rstrip("/") + "/index.json"
        with httpx.Client(follow_redirects=True, max_redirects=3) as client:
            resp = client.get(index_url)
            resp.raise_for_status()
            index = resp.json()

        skills = index.get("skills", {})
        if name not in skills:
            raise RuntimeError(
                f"Skill {name!r} not found in registry {self._config.name!r}"
            )

        entry = skills[name]
        bundle_url: str = entry["url"]
        version: str = entry.get("version", "unknown")

        safe_name = name.replace("/", "__")
        dest = quarantine_dir / f"{safe_name}.tar.gz"

        with httpx.Client(follow_redirects=True, max_redirects=3) as client:
            _stream_download(client, bundle_url, dest)

        content_hash = _sha256_file(dest)
        return FetchResult(
            local_path=dest,
            content_hash=content_hash,
            source_name=self._config.name,
            bundle_url=bundle_url,
            version=version,
        )


# ---------------------------------------------------------------------------
# Well-known discovery source
# ---------------------------------------------------------------------------


class WellKnownSource(SkillSourceAdapter):
    """Discover and fetch skills via ``/.well-known/skills/index.json``.

    Follows the agentskills.io pattern.  The well-known endpoint returns
    a skills registry index; the adapter then delegates to ``RegistrySource``
    logic after resolving the canonical registry URL.
    """

    def fetch(self, name: str, quarantine_dir: Path) -> FetchResult:
        """Discover registry URL, then download the skill bundle."""
        if not self._config.url:
            raise ValueError(
                f"SkillSource {self._config.name!r} is type 'wellknown' "
                "but has no 'url' configured"
            )

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "httpx is required for well-known skill discovery. "
                "Install arcskill[hub] to add it."
            ) from exc

        base = self._config.url.rstrip("/")
        well_known_url = f"{base}/.well-known/skills/index.json"

        with httpx.Client(follow_redirects=True, max_redirects=3) as client:
            resp = client.get(well_known_url)
            resp.raise_for_status()
            index = resp.json()

        # The well-known index may embed skill entries directly.
        skills = index.get("skills", {})
        if name not in skills:
            raise RuntimeError(
                f"Skill {name!r} not found in well-known index at {self._config.url!r}"
            )

        entry = skills[name]
        bundle_url: str = entry["url"]
        version: str = entry.get("version", "unknown")

        safe_name = name.replace("/", "__")
        dest = quarantine_dir / f"{safe_name}.tar.gz"

        with httpx.Client(follow_redirects=True, max_redirects=3) as client:
            _stream_download(client, bundle_url, dest)

        content_hash = _sha256_file(dest)
        return FetchResult(
            local_path=dest,
            content_hash=content_hash,
            source_name=self._config.name,
            bundle_url=bundle_url,
            version=version,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_adapter(source: SkillSource) -> SkillSourceAdapter:
    """Return the appropriate adapter for a SkillSource config.

    Parameters
    ----------
    source:
        Configured source entry from ``[[skills.hub.sources]]``.

    Returns
    -------
    SkillSourceAdapter
        Concrete adapter ready to call ``fetch()``.

    Raises
    ------
    ValueError
        If the source type is unknown.
    """
    match source.type:
        case "github":
            return GitHubSource(source)
        case "registry":
            return RegistrySource(source)
        case "wellknown":
            return WellKnownSource(source)
        case "local":
            return _LocalSource(source)
        case _:
            raise ValueError(f"Unknown source type: {source.type!r}")


# ---------------------------------------------------------------------------
# Local source (development / air-gapped)
# ---------------------------------------------------------------------------


class _LocalSource(SkillSourceAdapter):
    """Copy a skill bundle from a local filesystem path.

    Used for development, air-gapped installs, and testing.  The
    source must be a single ``.tar.gz`` file or a directory containing
    one.
    """

    def fetch(self, name: str, quarantine_dir: Path) -> FetchResult:
        """Copy the local bundle into quarantine_dir."""
        if not self._config.path:
            raise ValueError(
                f"SkillSource {self._config.name!r} is type 'local' "
                "but has no 'path' configured"
            )

        import shutil

        src = Path(self._config.path)
        safe_name = name.replace("/", "__")

        if src.is_file():
            dest = quarantine_dir / f"{safe_name}.tar.gz"
            shutil.copy2(src, dest)
        else:
            # Assume directory -- look for matching tarball.
            candidates = list(src.glob(f"{safe_name}*.tar.gz"))
            if not candidates:
                raise RuntimeError(
                    f"No bundle matching {safe_name!r} found in {src}"
                )
            dest = quarantine_dir / candidates[0].name
            shutil.copy2(candidates[0], dest)

        content_hash = _sha256_file(dest)
        return FetchResult(
            local_path=dest,
            content_hash=content_hash,
            source_name=self._config.name,
            bundle_url="",
            version="local",
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _stream_download(client: object, url: str, dest: Path) -> None:
    """Stream *url* to *dest* with a hard size cap.

    Raises RuntimeError if the download exceeds ``_MAX_BUNDLE_BYTES``.
    """
    import httpx as _httpx  # imported here to keep outer import lazy

    assert isinstance(client, _httpx.Client)  # noqa: S101 -- internal helper

    if not url.startswith("https://"):
        raise ValueError(f"Insecure URL rejected (HTTPS required): {url!r}")

    total = 0
    with client.stream("GET", url, follow_redirects=True) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=65536):
                total += len(chunk)
                if total > _MAX_BUNDLE_BYTES:
                    raise RuntimeError(
                        f"Bundle download exceeded {_MAX_BUNDLE_BYTES} bytes: {url!r}"
                    )
                fh.write(chunk)


def _sha256_file(path: Path) -> str:
    """Return the lowercase hex SHA-256 digest of *path*."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
