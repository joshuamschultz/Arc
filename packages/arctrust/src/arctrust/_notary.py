"""Reference out-of-process notary — signs stdin with a seed the caller never reads.

This is the child half of :class:`arctrust.signer.FileNotaryTransit`: a separate
OS process that loads the operator seed from the notary keystore and signs the
message handed to it on stdin, writing the raw signature to stdout. The seed is
read ONLY here — the calling (agent) process shells out and only ever handles
the message and the returned signature, never the seed (SPEC-037 REQ-006).

Run as: ``python -m arctrust._notary <seed_path> [algorithm]`` with the message
on stdin. ``algorithm`` is ``ed25519`` (default) or ``ecdsa-p256`` (F1).
"""

from __future__ import annotations

import sys
from pathlib import Path

from arctrust.signer import ECDSA_P256, ED25519, _ecdsa_sign


def main(argv: list[str]) -> int:
    """Sign stdin bytes with the seed at ``argv[1]``; emit the signature to stdout."""
    if len(argv) not in (2, 3):  # pragma: no cover — misuse guard
        sys.stderr.write("usage: python -m arctrust._notary <seed_path> [algorithm]\n")
        return 2
    algorithm = argv[2] if len(argv) == 3 else ED25519
    seed = Path(argv[1]).read_bytes()  # the ONLY read of the seed, in this process
    message = sys.stdin.buffer.read()
    if algorithm == ECDSA_P256:
        signature = _ecdsa_sign(seed, message)
    else:
        from nacl.signing import SigningKey

        signature = SigningKey(seed).sign(message).signature
    sys.stdout.buffer.write(signature)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover — process entry point
    raise SystemExit(main(sys.argv))
