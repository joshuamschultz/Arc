"""UJ-6 — a connector package goes from an uploaded archive to a signed, installed bundle.

These pin the operator's path through :mod:`arcagent.extension.bundle_import`: unpack into
staging, review everything the package declares, sign the reviewed bytes with the operator
key, install where the loader trusts it, update with a diff, and remove only when unused.
The abuse cases live in ``tests/security/test_bundle_import_abuse.py``.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust import bootstrap_operator_signer
from arctrust.audit import AuditEvent
from arctrust.paths import installed_extensions_dir

from arcagent.capabilities import artifact_signing
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.connections import Connections
from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.bundle_import import (
    BundleStaging,
    installed_bundles,
    remove_installed_bundle,
)
from arcagent.extension.catalog import ExtensionCatalog, resolve_extension_roots
from arcagent.extension.grants import Connection, ConnectionRegistry
from arcagent.extension.loader import ExtensionLoader

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "notes_connector"


class ListSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def fixture_files() -> dict[str, bytes]:
    return {
        path.relative_to(FIXTURE).as_posix(): path.read_bytes()
        for path in sorted(FIXTURE.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def make_zip(files: dict[str, bytes], *, wrapper: str = "") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(f"{wrapper}{name}", data)
    return buffer.getvalue()


def make_tar_gz(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arc_dir = tmp_path / "arc"
    monkeypatch.setenv("ARC_TEAM_ROOT", str(arc_dir))
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "dot-arc"))
    monkeypatch.delenv("ARC_EXTENSIONS_ROOT", raising=False)
    return arc_dir


def connections_for(arc_dir: Path) -> Connections:
    backend = FakeBackend()

    async def opener() -> Any:
        return backend

    return Connections.for_deployment(arc_dir=arc_dir, state_opener=opener)


def upload(tmp_path: Path, data: bytes, name: str = "notes.zip") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_upload_is_staged_and_reviewed_without_installing(
    deployment: Path, tmp_path: Path
) -> None:
    staging = BundleStaging()
    sink = ListSink()

    staged = staging.stage_archive(
        upload(tmp_path, make_zip(fixture_files(), wrapper="notes-1.0.0/")),
        connections=connections_for(deployment),
        audit_sink=sink,
    )

    review = staged.review
    assert review.name == "notes"
    assert review.version == "1.0.0"
    assert review.attachment == "native"
    assert review.publisher.status == "unsigned"
    assert review.confirm_required is True
    assert [(t.name, t.classification, t.network) for t in review.tools] == [
        ("notes_echo", "read_only", False)
    ]
    assert [(s.name, s.sensitive, s.required) for s in review.secrets] == [
        ("api_token", True, True)
    ]
    assert review.executes_code is True
    assert {f.path for f in review.files} == {
        "extension.toml",
        "arc_ext_notes/__init__.py",
        "skills/notes/SKILL.md",
    }
    assert next(f for f in review.files if f.path.endswith(".py")).executes is True
    assert review.skills == ("notes",)
    assert review.update is None
    assert any("runs its own code" in flag for flag in review.flags)
    assert len(review.digest) == 64
    # Nothing is installed, and nothing is on the search path, until approval.
    assert not (installed_extensions_dir() / "notes").exists()
    assert [e.action for e in sink.events] == ["connector.bundle_staged"]


def test_a_tar_gz_package_reviews_the_same_as_a_zip(deployment: Path, tmp_path: Path) -> None:
    staging = BundleStaging()
    connections = connections_for(deployment)
    zipped = staging.stage_archive(
        upload(tmp_path, make_zip(fixture_files())), connections=connections, audit_sink=ListSink()
    )
    tarred = staging.stage_archive(
        upload(tmp_path, make_tar_gz(fixture_files()), "notes.tar.gz"),
        connections=connections,
        audit_sink=ListSink(),
    )
    assert tarred.review.digest == zipped.review.digest


def test_approve_signs_installs_and_the_loader_accepts_it(
    deployment: Path, tmp_path: Path
) -> None:
    connections = connections_for(deployment)
    staging = BundleStaging()
    staged = staging.stage_archive(
        upload(tmp_path, make_zip(fixture_files())), connections=connections, audit_sink=ListSink()
    )

    installed = staging.approve(
        staged.staging_id, confirm_name="notes", connections=connections, audit_sink=ListSink()
    )

    target = installed_extensions_dir() / "notes"
    assert installed.name == "notes" and installed.path == target
    assert installed.signer_did.startswith("did:")
    for path in (target / "extension.toml", target / "arc_ext_notes" / "__init__.py"):
        assert artifact_signing.verify_file(
            path, path.read_bytes(), trusted_public_key=connections._pinned_key()
        )
    # The staging is spent: approving twice is refused, not a second install.
    with pytest.raises(ExtensionError) as again:
        staging.approve(
            staged.staging_id, confirm_name="notes", connections=connections, audit_sink=ListSink()
        )
    assert again.value.details["reason"] == "expired"
    catalog = ExtensionCatalog(
        roots=resolve_extension_roots(deployment), tier=Tier.PERSONAL, audit_sink=ListSink()
    )
    assert catalog.locate("notes") == target


async def test_installed_bundle_loads_through_the_extension_loader(
    deployment: Path, tmp_path: Path
) -> None:
    connections = connections_for(deployment)
    staging = BundleStaging()
    staged = staging.stage_archive(
        upload(tmp_path, make_zip(fixture_files())), connections=connections, audit_sink=ListSink()
    )
    staging.approve(
        staged.staging_id, confirm_name="notes", connections=connections, audit_sink=ListSink()
    )
    loader = ExtensionLoader(
        roots=resolve_extension_roots(deployment),
        registry=CapabilityRegistry(),
        tier=Tier.PERSONAL,
        audit_sink=ListSink(),
        trusted_public_key=connections._pinned_key(),
    )
    loaded = await loader.load("notes")
    assert loaded.path == installed_extensions_dir() / "notes"


def test_a_wrong_confirmation_name_is_refused(deployment: Path, tmp_path: Path) -> None:
    connections = connections_for(deployment)
    staging = BundleStaging()
    staged = staging.stage_archive(
        upload(tmp_path, make_zip(fixture_files())), connections=connections, audit_sink=ListSink()
    )
    with pytest.raises(ExtensionError) as refused:
        staging.approve(
            staged.staging_id, confirm_name="note", connections=connections, audit_sink=ListSink()
        )
    assert refused.value.details["reason"] == "confirm_mismatch"
    assert not (installed_extensions_dir() / "notes").exists()


def test_a_package_signed_by_the_operator_is_a_verified_publisher(
    deployment: Path, tmp_path: Path
) -> None:
    connections = connections_for(deployment)
    bootstrap_operator_signer(base=deployment)
    signed_dir = tmp_path / "signed" / "notes"
    for name, data in fixture_files().items():
        (signed_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (signed_dir / name).write_bytes(data)
    connections.sign_bundle(signed_dir)
    files = {
        p.relative_to(signed_dir).as_posix(): p.read_bytes()
        for p in signed_dir.rglob("*")
        if p.is_file()
    }

    staged = BundleStaging().stage_archive(
        upload(tmp_path, make_zip(files)), connections=connections, audit_sink=ListSink()
    )

    assert staged.review.publisher.status == "verified"
    assert staged.review.publisher.signer_did.startswith("did:")
    assert staged.review.confirm_required is False
    # The signatures that came with the package are not part of what is reviewed.
    assert all(not f.path.endswith(".arcsig") for f in staged.review.files)


def test_update_shows_a_diff_and_needs_confirmation(deployment: Path, tmp_path: Path) -> None:
    connections = connections_for(deployment)
    staging = BundleStaging()
    first = staging.stage_archive(
        upload(tmp_path, make_zip(fixture_files())), connections=connections, audit_sink=ListSink()
    )
    staging.approve(
        first.staging_id, confirm_name="notes", connections=connections, audit_sink=ListSink()
    )

    files = fixture_files()
    manifest = files["extension.toml"].decode()
    manifest = manifest.replace('version = "1.0.0"', 'version = "1.1.0"')
    manifest = manifest.replace(
        'description = "Echo a note back from the Notes service."',
        'description = "Echo a note back, louder."',
    )
    manifest = manifest.replace(
        '[[secrets]]\nname = "api_token"',
        '[[secrets]]\nname = "region"\nprompt = "Region"\nsensitive = false\n\n'
        '[[secrets]]\nname = "api_token"',
    )
    files["extension.toml"] = manifest.encode()
    second = staging.stage_archive(
        upload(tmp_path, make_zip(files), "notes-1.1.zip"),
        connections=connections,
        audit_sink=ListSink(),
    )

    diff = second.review.update
    assert diff is not None
    assert diff.installed_version == "1.0.0"
    assert diff.tools_changed == ("notes_echo",)
    assert diff.tools_added == () and diff.tools_removed == ()
    assert diff.new_secrets == ("region",)
    assert second.review.confirm_required is True
    installed = staging.approve(
        second.staging_id, confirm_name="notes", connections=connections, audit_sink=ListSink()
    )
    assert installed.version == "1.1.0"


def test_installed_listing_names_who_uses_each_bundle(deployment: Path, tmp_path: Path) -> None:
    connections = connections_for(deployment)
    staging = BundleStaging()
    staged = staging.stage_archive(
        upload(tmp_path, make_zip(fixture_files())), connections=connections, audit_sink=ListSink()
    )
    staging.approve(
        staged.staging_id, confirm_name="notes", connections=connections, audit_sink=ListSink()
    )
    ConnectionRegistry(deployment).define("work_notes", Connection(extension="notes"))

    listing = installed_bundles(connections)

    assert [(b.name, b.version, b.used_by) for b in listing] == [
        ("notes", "1.0.0", ("work_notes",))
    ]


def test_remove_refuses_while_a_connection_uses_it(deployment: Path, tmp_path: Path) -> None:
    connections = connections_for(deployment)
    staging = BundleStaging()
    staged = staging.stage_archive(
        upload(tmp_path, make_zip(fixture_files())), connections=connections, audit_sink=ListSink()
    )
    staging.approve(
        staged.staging_id, confirm_name="notes", connections=connections, audit_sink=ListSink()
    )
    registry = ConnectionRegistry(deployment)
    registry.define("work_notes", Connection(extension="notes"))

    with pytest.raises(ExtensionError) as in_use:
        remove_installed_bundle("notes", connections=connections, audit_sink=ListSink())
    assert in_use.value.details["reason"] == "in_use"
    assert in_use.value.details["used_by"] == ["work_notes"]
    assert (installed_extensions_dir() / "notes").is_dir()

    registry.forget("work_notes")
    sink = ListSink()
    remove_installed_bundle("notes", connections=connections, audit_sink=sink)
    assert not (installed_extensions_dir() / "notes").exists()
    assert [e.action for e in sink.events] == ["connector.bundle_removed"]


def test_remove_never_touches_a_bundle_arc_ships(deployment: Path) -> None:
    with pytest.raises(ExtensionError) as refused:
        remove_installed_bundle(
            "sqlite", connections=connections_for(deployment), audit_sink=ListSink()
        )
    assert refused.value.details["reason"] == "not_installed"


def test_discarded_staging_is_gone(deployment: Path, tmp_path: Path) -> None:
    staging = BundleStaging()
    staged = staging.stage_archive(
        upload(tmp_path, make_zip(fixture_files())),
        connections=connections_for(deployment),
        audit_sink=ListSink(),
    )
    assert staging.discard(staged.staging_id) is True
    assert staging.discard(staged.staging_id) is False
    assert not any((installed_extensions_dir() / ".staging").glob("*/notes"))


def test_staging_expires_after_an_hour(deployment: Path, tmp_path: Path) -> None:
    now = [1000.0]
    staging = BundleStaging(clock=lambda: now[0])
    connections = connections_for(deployment)
    staged = staging.stage_archive(
        upload(tmp_path, make_zip(fixture_files())), connections=connections, audit_sink=ListSink()
    )
    now[0] += 3601
    with pytest.raises(ExtensionError) as expired:
        staging.approve(
            staged.staging_id, confirm_name="notes", connections=connections, audit_sink=ListSink()
        )
    assert expired.value.details["reason"] == "expired"
    assert not (installed_extensions_dir() / ".staging" / staged.staging_id).exists()


def test_a_code_bundle_in_the_operator_tree_can_be_staged_for_signing(
    deployment: Path,
) -> None:
    planted = deployment / "extensions" / "notes"
    for name, data in fixture_files().items():
        (planted / name).parent.mkdir(parents=True, exist_ok=True)
        (planted / name).write_bytes(data)
    connections = connections_for(deployment)

    staging = BundleStaging()
    staged = staging.stage_local("notes", connections=connections, audit_sink=ListSink())

    assert staged.review.name == "notes"
    staging.approve(
        staged.staging_id, confirm_name="notes", connections=connections, audit_sink=ListSink()
    )
    catalog = ExtensionCatalog(
        roots=resolve_extension_roots(deployment), tier=Tier.PERSONAL, audit_sink=ListSink()
    )
    assert catalog.locate("notes") == installed_extensions_dir() / "notes"


def _plant_in_operator_tree(deployment: Path) -> Path:
    planted = deployment / "extensions" / "notes"
    for name, data in fixture_files().items():
        (planted / name).parent.mkdir(parents=True, exist_ok=True)
        (planted / name).write_bytes(data)
    return planted


def test_catalog_lists_an_unsigned_code_bundle_as_waiting_for_signing(deployment: Path) -> None:
    _plant_in_operator_tree(deployment)
    catalog = ExtensionCatalog(
        roots=resolve_extension_roots(deployment), tier=Tier.PERSONAL, audit_sink=ListSink()
    )

    entry = next(e for e in catalog.available() if e.name == "notes")

    assert "review and sign" in entry.error
    assert "arc " not in entry.error
    assert entry.action == "sign_bundle"


def test_connecting_an_unsigned_code_bundle_asks_for_signing(deployment: Path) -> None:
    _plant_in_operator_tree(deployment)
    catalog = ExtensionCatalog(
        roots=resolve_extension_roots(deployment), tier=Tier.PERSONAL, audit_sink=ListSink()
    )
    with pytest.raises(ExtensionError) as refused:
        catalog.locate("notes")
    assert refused.value.details["reason"] == "code_in_operator_tree"
    assert refused.value.details["action"] == "sign_bundle"
    assert "arc " not in refused.value.message


def test_staging_left_by_an_earlier_process_is_swept_after_an_hour(
    deployment: Path, tmp_path: Path
) -> None:
    import os
    import time

    root = installed_extensions_dir() / ".staging"
    stale = root / ("a" * 32) / "notes"
    fresh = root / ("b" * 32) / "notes"
    for folder in (stale, fresh):
        folder.mkdir(parents=True)
        (folder / "extension.toml").write_text("x")
    old = time.time() - 3700
    os.utime(stale.parent, (old, old))

    BundleStaging().get("c" * 32)

    assert not stale.parent.exists()
    assert fresh.parent.exists()
