"""Path and name guards — the difference between a module and an arbitrary write.

Every string a manifest declares is eventually joined onto a deployment root by
a privileged, operator-run install. A single ``..`` segment, a leading ``/``, or
a Windows drive letter surviving to that join turns a bundle into a write
primitive against anything the installing user owns (ASI05/ASI06).

The guards are therefore applied where the value is *constructed* rather than
where it is used, so no caller — present or future — can route around them.
"""

from __future__ import annotations

import ntpath
import posixpath
import re

from arcbundle.errors import BundleManifestError

__all__ = ["require_relative_path", "require_safe_name"]

# A module name becomes one directory component under the deployment root. No
# dots at all, so "." and ".." cannot be spelled and no extension is implied.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_FORBIDDEN_SEGMENTS = frozenset({"", ".", ".."})


def require_safe_name(name: str, *, field: str) -> str:
    """Return ``name`` if it is usable as exactly one directory component."""
    if not _SAFE_NAME.match(name):
        raise BundleManifestError(
            f"{field} must be a single safe path component matching "
            f"{_SAFE_NAME.pattern!r}, got {name!r}"
        )
    return name


def require_relative_path(path: str, *, field: str) -> str:
    """Return ``path`` if it stays inside its own root, else refuse it.

    Refuses, in order: the empty string; a backslash (a separator on Windows and
    a legal filename character on POSIX, so it can mean two different trees);
    a drive or UNC prefix; an absolute path; any empty, ``.`` or ``..`` segment;
    and finally anything that is not already its own normalized form — the last
    check is what catches an escape spelled in a way the segment scan below
    would read as ordinary, rather than only a literal leading ``..``.
    """
    if not path:
        raise BundleManifestError(f"{field} must not be empty")
    if "\\" in path:
        raise BundleManifestError(f"{field} must not contain a backslash, got {path!r}")
    if ntpath.splitdrive(path)[0]:
        raise BundleManifestError(f"{field} must not carry a drive or UNC prefix, got {path!r}")
    if posixpath.isabs(path) or ntpath.isabs(path):
        raise BundleManifestError(f"{field} must be relative, got {path!r}")
    for segment in path.split("/"):
        if segment in _FORBIDDEN_SEGMENTS:
            raise BundleManifestError(f"{field} must not contain {segment!r} segments: {path!r}")
    if posixpath.normpath(path) != path:
        raise BundleManifestError(f"{field} must already be normalized, got {path!r}")
    return path
