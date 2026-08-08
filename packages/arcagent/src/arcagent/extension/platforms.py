"""SPEC-064 — the one spelling of "which machine is this", shared by pin and install.

A digest is only meaningful for the platform whose bytes it covers, so the
component that VERIFIES a pinned artifact and the one that FETCHES it have to
agree on the key a manifest writes. Two spellings would mean a bundle pinning
``linux/arm64`` while a verifier looked up ``linux/aarch64`` — a pin that covers
nothing, which is worse than no pin, because it reads as one.

Detection is injectable nowhere and constant here on purpose: a caller able to
say which platform it "is" could ask for the digest of a build it is not going
to run.
"""

from __future__ import annotations

import platform

#: The key a platform-independent artifact is pinned under. An npm tarball or a
#: PyPI sdist is the same bytes on every machine, so it names none.
ANY_PLATFORM = "any"

#: Every spelling a machine reports for the two architectures Arc ships on,
#: normalised to the one a release asset is named after.
_ARCHITECTURES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def host_platform() -> str:
    """This machine as a manifest spells it — ``linux/arm64``, ``darwin/amd64``.

    An architecture with no normalised name is returned as the machine reports
    it, so an unusual host misses every pin and is refused by name rather than
    silently matching the wrong one.
    """
    machine = platform.machine().lower()
    return f"{platform.system().lower()}/{_ARCHITECTURES.get(machine, machine)}"


__all__ = ["ANY_PLATFORM", "host_platform"]
