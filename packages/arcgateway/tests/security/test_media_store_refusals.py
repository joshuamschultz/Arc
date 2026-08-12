"""MediaStore's two refusals, and the guarantee that they leave nothing behind.

SPEC-065 review: both branches were reachable and neither was exercised. The
containment check is the one the module's own docstring calls "the check that
cannot be talked around" — a regression in ``_sanitise`` or ``_compose_name``
would be caught by nothing else, and a green suite would say so confidently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcgateway.media_store import _MAX_COLLISIONS, MediaStore, MediaTooLargeError


def _store(tmp_path: Path, *, max_bytes: int = 1024) -> MediaStore:
    return MediaStore(workspace=tmp_path / "workspace", max_bytes=max_bytes)


def test_a_composed_path_that_escapes_the_inbox_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ancestry, not string prefixes, is what makes the fence hold.

    Sanitisation is supposed to make escape unreachable, so the only honest way
    to reach the guard is to break the composer — which is exactly the
    regression the guard exists to survive.
    """
    import arcgateway.media_store as media_store

    monkeypatch.setattr(
        media_store, "_compose_name", lambda **_kwargs: "../../escaped.bin"
    )

    with pytest.raises(ValueError, match="escaped the inbox"):
        _store(tmp_path).store(
            data=b"x",
            declared_name="x.bin",
            mime="application/octet-stream",
            kind="file",
            sender="did:arc:user",
            channel="telegram:1",
            actor_did="did:arc:user",
        )

    assert not (tmp_path / "escaped.bin").exists(), "the refused artefact was written anyway"


def test_a_refused_oversize_artefact_leaves_no_file_behind(tmp_path: Path) -> None:
    """The ceiling is decided before anything is opened (REQ-299)."""
    store = _store(tmp_path, max_bytes=4)
    workspace = tmp_path / "workspace"

    with pytest.raises(MediaTooLargeError) as excinfo:
        store.store(
            data=b"12345",
            declared_name="big.bin",
            mime="application/octet-stream",
            kind="file",
            sender="did:arc:user",
            channel="telegram:1",
            actor_did="did:arc:user",
        )

    assert excinfo.value.size_bytes == 5
    assert excinfo.value.limit_bytes == 4
    assert not list(workspace.rglob("*.bin")), "a refused artefact reached the disk"


def test_an_artefact_exactly_at_the_ceiling_is_accepted(tmp_path: Path) -> None:
    """Inclusive, as documented — an off-by-one here refuses valid files."""
    stored = _store(tmp_path, max_bytes=4).store(
        data=b"1234",
        declared_name="exact.bin",
        mime="application/octet-stream",
        kind="file",
        sender="did:arc:user",
        channel="telegram:1",
        actor_did="did:arc:user",
    )
    assert stored.size_bytes == 4


def test_the_collision_loop_gives_up_rather_than_spinning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flood must not spin here forever; it must fail and say so."""
    import arcgateway.media_store as media_store

    # Every ordinal composes the same name, so every attempt collides.
    monkeypatch.setattr(media_store, "_compose_name", lambda **_kwargs: "same.bin")

    store = _store(tmp_path)
    kwargs = {
        "declared_name": "same.bin",
        "mime": "application/octet-stream",
        "kind": "file",
        "sender": "did:arc:user",
        "channel": "telegram:1",
        "actor_did": "did:arc:user",
    }
    store.store(data=b"first", **kwargs)  # type: ignore[arg-type]

    with pytest.raises(FileExistsError, match=str(_MAX_COLLISIONS)):
        store.store(data=b"second", **kwargs)  # type: ignore[arg-type]


def test_the_stored_ref_is_relative_to_the_workspace(tmp_path: Path) -> None:
    """A reference outlives the machine that made it (review finding)."""
    stored = _store(tmp_path).store(
        data=b"bytes",
        declared_name="chart.png",
        mime="image/png",
        kind="image",
        sender="did:arc:user",
        channel="telegram:1",
        actor_did="did:arc:user",
    )

    assert not Path(stored.ref).is_absolute(), f"ref is an absolute host path: {stored.ref}"
    assert stored.ref.startswith("inbox/")
    assert str(tmp_path) not in stored.ref, "the host's filesystem layout leaked into the ref"
    assert (tmp_path / "workspace" / stored.ref).read_bytes() == b"bytes"
