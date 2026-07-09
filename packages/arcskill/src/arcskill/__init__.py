"""arcskill — Skill management hub for Arc.

Validates, installs, scans, and locks skills for use by agents, run loops,
and LLM contexts. Public surface is exposed under ``arcskill.hub`` and
``arcskill.lock``.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("arcskill")
except PackageNotFoundError:  # reason: source checkout without an installed distribution
    __version__ = "0.2.0"
