"""arccmd — Unified CLI for Arc products."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("arccmd")
except PackageNotFoundError:  # reason: source checkout without an installed distribution
    __version__ = "0.7.0"
