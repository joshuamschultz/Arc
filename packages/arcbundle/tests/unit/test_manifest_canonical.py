"""T-956 (RED) — REQ-325: the manifest's canonical bytes are stable, and they
cannot drift from the encoder `arcrun` already signs backend manifests with.

A signature only means something if the signer and every verifier serialize the
same object to the same bytes. Arc now has two places that canonicalize a signed
manifest — `arcrun.backends._verifier.canonical_json_payload` and
`arcbundle`'s `BundleManifest.canonical_bytes()`. If either one ever picks
different separators, drops `sort_keys`, or turns off `ensure_ascii`, bundles
signed by one side stop verifying on the other, and the failure shows up as an
unexplained signature rejection in the field rather than as a test.

The guard is a pivot: both encoders are asserted byte-identical to the single
shared primitive, `arctrust.canonical_json`. Equal to the same thing means equal
to each other, and a change to either one breaks its own assertion here.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import textwrap
from typing import Any

from arcrun.backends._verifier import canonical_json_payload
from arctrust import canonical_json

from arcbundle import BundleManifest, FileEntry

# Sorted-key order for these five fields is files, format_version, issuer,
# module, version — asserted below, so a silent loss of sort_keys fails loudly.
_FIELDS: dict[str, Any] = {
    "format_version": 1,
    "module": "browser",
    "version": "1.0.0",
    "issuer": "did:arc:release",
}

_FILES = [
    ("_runtime.py", "a" * 64),
    ("tools/fetch.py", "b" * 64),
]


def _manifest() -> BundleManifest:
    return BundleManifest(
        **_FIELDS,
        files=[FileEntry(path=path, sha256=digest) for path, digest in _FILES],
    )


def test_canonical_bytes_ignore_the_order_fields_were_supplied_in() -> None:
    """Two manifests built from the same values encode to the same bytes.

    Key order is the classic way a canonical encoder silently forks: one caller
    builds the object one way, another builds it another way, and only the
    signature check downstream notices.
    """
    forward = BundleManifest(
        format_version=1,
        module="browser",
        version="1.0.0",
        issuer="did:arc:release",
        files=[FileEntry(path=path, sha256=digest) for path, digest in _FILES],
    )
    reverse = BundleManifest.model_validate(
        {
            "files": [{"sha256": digest, "path": path} for path, digest in _FILES],
            "issuer": "did:arc:release",
            "version": "1.0.0",
            "module": "browser",
            "format_version": 1,
        }
    )

    assert forward.canonical_bytes() == reverse.canonical_bytes()


def test_canonical_bytes_sort_keys_and_use_compact_separators() -> None:
    """The encoding convention itself, asserted on the bytes."""
    encoded = _manifest().canonical_bytes()

    assert encoded.startswith(b'{"files":[{"path":'), encoded[:64]
    assert b", " not in encoded
    assert b'": ' not in encoded


def test_canonical_bytes_are_pure_ascii_so_the_utf8_encoding_is_locale_stable() -> None:
    """A non-ASCII payload path escapes rather than emitting raw UTF-8.

    `ensure_ascii=False` would produce different bytes for the same manifest on
    a differently configured box — a signature that verifies on the build host
    and fails on the target.
    """
    manifest = BundleManifest(
        **_FIELDS,
        files=[FileEntry(path="módulo/data.txt", sha256="c" * 64)],
    )
    encoded = manifest.canonical_bytes()

    assert encoded.decode("ascii") == encoded.decode("utf-8")
    assert b"\\u00f3" in encoded


def test_canonical_bytes_are_identical_in_a_second_process() -> None:
    """Byte stability across process runs, proven by a real second interpreter.

    An in-process loop cannot see anything that varies per process — hash seed,
    interpreter state, import order. Only another run can.
    """
    source = textwrap.dedent(
        """
        import hashlib

        from arcbundle import BundleManifest, FileEntry

        manifest = BundleManifest(
            format_version=1,
            module="browser",
            version="1.0.0",
            issuer="did:arc:release",
            files=[
                FileEntry(path="_runtime.py", sha256="a" * 64),
                FileEntry(path="tools/fetch.py", sha256="b" * 64),
            ],
        )
        print(hashlib.sha256(manifest.canonical_bytes()).hexdigest())
        """
    )
    completed = subprocess.run(  # noqa: S603 — fixed argv, no shell, our own interpreter
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=True,
    )

    expected = hashlib.sha256(_manifest().canonical_bytes()).hexdigest()
    assert completed.stdout.strip() == expected


def test_encoding_cannot_drift_from_the_arcrun_backend_verifier() -> None:
    """Both encoders sit on `arctrust.canonical_json` for an equivalent payload.

    The arcrun side signs `{"meta": ..., "backends": [...]}`; the arcbundle side
    signs the manifest mapping. Different shapes, one encoder — so the parity
    that matters is that neither has its own serialization opinion.
    """
    manifest = _manifest()
    mapping = manifest.model_dump(mode="json")

    # arcbundle side.
    assert manifest.canonical_bytes() == canonical_json(mapping)

    # arcrun side, fed the same field values so the comparison is about encoding
    # rather than about content.
    meta = {key: value for key, value in mapping.items() if key != "files"}
    files = mapping["files"]
    assert canonical_json_payload(meta=meta, backends=files) == canonical_json(
        {"meta": meta, "backends": files}
    )
