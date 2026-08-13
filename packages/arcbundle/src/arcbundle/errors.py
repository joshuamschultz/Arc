"""The refusal vocabulary of a bundle install — one exception per failed gate.

Deliberately NOT rooted in ``ValueError``. Pydantic v2 converts a ``ValueError``
raised inside a validator into a ``ValidationError``; every other exception type
propagates untouched. The path and name checks that keep a manifest from
becoming an arbitrary-write primitive live *in the model*, so their refusal has
to survive that conversion and reach the caller by its own name.

Every error here means the same thing operationally: nothing was installed.
"""

from __future__ import annotations

__all__ = [
    "BundleContentHashError",
    "BundleError",
    "BundleManifestError",
    "BundleMaterializeError",
    "BundleSignatureError",
]


class BundleError(Exception):
    """Base for every bundle refusal — catch this to exit non-zero on any of them."""


class BundleManifestError(BundleError):
    """The manifest is malformed, non-canonical, or declares an unsafe path."""


class BundleSignatureError(BundleError):
    """The manifest signature is absent, invalid, or from an issuer this tier does not trust."""


class BundleContentHashError(BundleError):
    """A declared payload file is missing, altered, or the tree carries an undeclared file."""


class BundleMaterializeError(BundleError):
    """An atomic write or an inverse remove did not complete."""
