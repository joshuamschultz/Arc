"""arctrust is the trust leaf; it must not know what a chat product is.

A field named ``telegram_user_id`` here would put one vendor's surface inside
the cryptographic foundation every other package depends on — and the next
platform would need a schema change to the identity store. Pairings are
``{platform: external_id}`` with the platform an opaque string supplied by
whichever gateway package owns that surface.

This is not covered by ``test_layering.py``: the violation is domain knowledge,
not an import, so nothing would have failed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "arctrust"

# Platforms Arc has or plans to have a gateway package for. "Signal" is left
# out deliberately: the word means something else in this package ("a divergence
# signal"), so including it would fail on prose rather than on coupling.
_PLATFORM_NAMES = ("telegram", "slack", "mattermost", "discord", "whatsapp")


@pytest.mark.parametrize("platform", _PLATFORM_NAMES)
def test_arctrust_never_names_a_chat_platform(platform: str) -> None:
    offenders = [
        path.relative_to(_SRC).as_posix()
        for path in _SRC.rglob("*.py")
        if platform in path.read_text(encoding="utf-8").lower()
    ]
    assert not offenders, (
        f"arctrust must not mention {platform!r} — found in {offenders}. "
        f"Surface identities belong in User.pairings, keyed by an opaque platform "
        f"string the owning gateway package supplies."
    )
