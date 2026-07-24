"""SigningAuthority seam — resolve a signing identity from the request principal (COMP-011).

Overlay prompts are signed artifacts: an operator edit is written as a markdown
overlay plus a detached ``.arcsig`` sidecar the agent's ``PromptResolver``
re-verifies at run start. arcui must therefore sign on the operator's behalf.

The single rule this module exists to enforce: **the signer is resolved from the
authenticated principal on the request, never hardcoded.** Today arcui has no
per-user login, so an operator-role caller signs with the deployment operator key
(the same ``~/.arc/operator`` key the approval routes sign with) and the recorded
signer DID is that key's canonically-derived operator DID. Under a future per-user
login, :func:`signer_for` returns that user's key and DID with **zero edits at any
call site** — every handler goes through this seam, so the resolution swap is
local to one function. This is the SPEC-017 "audit-lies" guard made structural:
the DID that signs is the DID that is audited, and both come from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from arctrust import OperatorKey, default_operator_key_path
from arctrust.artifact import ArtifactSignature, sign_artifact
from arctrust.policy import OperatorApprovalAuthority
from starlette.requests import Request


class SigningUnavailableError(RuntimeError):
    """No signing identity could be resolved for the request principal.

    Raised when the caller is not an authenticated operator, or the on-box
    operator key is absent/unreadable. The write handler maps this to a refusal
    (never a silent unsigned write) — an overlay without a valid signature would
    be rejected by the agent's resolver anyway.
    """


@dataclass(frozen=True)
class SigningIdentity:
    """A resolved signing principal: its DID plus the seed used to sign with it.

    ``seed`` is a live Ed25519 private key seed; it is excluded from ``repr`` so
    it never lands in a log line or traceback frame.
    """

    did: str
    seed: bytes = field(repr=False)


def signer_for(request: Request) -> SigningIdentity:
    """Resolve the signing identity for the request's authenticated principal.

    The only place a key path or DID is resolved. Requires an operator-role
    principal (the write handler gates on this first; the check here is
    defense-in-depth so no future caller can sign as a viewer). Loads the on-box
    operator key read-only and derives its canonical operator DID — identical to
    the DID the approval routes and the ``arc`` CLI attribute grants to.
    """
    if getattr(request.state, "role", None) != "operator":
        raise SigningUnavailableError("signing requires an authenticated operator principal")
    try:
        operator_key = OperatorKey.load(default_operator_key_path(), generate_if_absent=False)
    except (OSError, ValueError, RuntimeError) as exc:
        raise SigningUnavailableError(f"operator key unavailable: {type(exc).__name__}") from exc
    did = OperatorApprovalAuthority(operator_key.into_signer()).did
    return SigningIdentity(did=did, seed=operator_key.seed)


def sign(content: bytes, identity: SigningIdentity) -> ArtifactSignature:
    """Sign ``content`` under ``identity`` and return the detached signature manifest."""
    return sign_artifact(content, signer_did=identity.did, private_key=identity.seed)


__all__ = ["SigningIdentity", "SigningUnavailableError", "sign", "signer_for"]
