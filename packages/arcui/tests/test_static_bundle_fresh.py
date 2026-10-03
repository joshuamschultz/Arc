"""The committed dashboard bundle must be at least as new as the sources it is built from.

``packages/arcui/src/arcui/static`` is the built output of ``packages/arcui/web``.
Both are tracked, and a frontend change that is committed without rebuilding ships
the *old* UI (the deploy stamp hashes the bundle, so a stale bundle looks like no
change at all). This test fails whenever the last commit touching the web sources
is newer than the last commit touching the bundle, so a release cannot go out with
a UI that does not match its source.

It reads git history only: it never runs ``npm``. The fix when it fails is one
``npm run build`` in ``packages/arcui/web`` and a commit of ``static/``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
STATIC = "packages/arcui/src/arcui/static"
#: Everything ``vite build`` reads. A change to any of these changes the bundle.
WEB_INPUTS = (
    "packages/arcui/web/src",
    "packages/arcui/web/public",
    "packages/arcui/web/index.html",
    "packages/arcui/web/package.json",
    "packages/arcui/web/package-lock.json",
    "packages/arcui/web/vite.config.ts",
    "packages/arcui/web/tsconfig.json",
    "packages/arcui/web/tsconfig.app.json",
)


def _git(*args: str) -> str:
    return subprocess.run(  # fixed git argv, no shell, repo-relative constants
        ["git", *args],  # noqa: S607  # dev tool via PATH
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _last_commit(*paths: str) -> tuple[int, str]:
    """Return ``(unix time, short sha)`` of the newest commit touching ``paths``."""
    out = _git("log", "-1", "--format=%ct %h", "--", *paths).split()
    assert out, f"no commit touches {paths}: the bundle or its sources are not tracked"
    return int(out[0]), out[1]


def test_committed_static_bundle_is_not_older_than_the_web_sources() -> None:
    source_time, source_sha = _last_commit(*WEB_INPUTS)
    bundle_time, bundle_sha = _last_commit(STATIC)

    assert bundle_time >= source_time, (
        f"static bundle ({bundle_sha}) is older than web sources ({source_sha}): "
        "run `npm run build` in packages/arcui/web and commit packages/arcui/src/arcui/static"
    )


def test_working_tree_web_sources_match_the_commit() -> None:
    """An uncommitted web edit is an unbuilt bundle waiting to happen."""
    dirty = _git("status", "--porcelain", "--", *WEB_INPUTS).strip()

    assert not dirty, f"uncommitted web source changes:\n{dirty}"
