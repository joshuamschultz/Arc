"""The stored credential must reach the connector that declared it.

``arc connector add`` collects a bundle's ``[[secrets]]``, writes them to the
:class:`~arcagent.extension.secrets.SecretStore` — and, until this suite existed,
handed the attachment ``{"bundle": ...}`` and nothing else. Every declared
credential was stored correctly and delivered nowhere, which made the whole
connector feature unusable and made the honest error message read like a to-do
list for the operator.

So these tests are written against the DELIVERY, never against the shape of a
call. Asserting that ``build_attachment`` was invoked with a mapping proves
nothing: the defect was a producer that was never connected to a seam that
already existed, and a mock of that seam would have been green throughout. Every
test here stores a real credential in a real store and then asks the extension's
own implementation what it received.

Three properties are load-bearing:

* **The value crosses exactly one boundary.** A :class:`~arcagent.extension.
  secrets.Secret` renders ``Secret(***)`` everywhere it is formatted, and that
  protection ends at ``reveal()``. It is revealed in ``build_attachment`` and
  nowhere earlier, so :func:`test_a_revealed_credential_never_reaches_a_rendered_
  string` asserts a sentinel is absent from every string a surface can show:
  the probe detail, the tool list, the audit chain, and a raised refusal.
* **A missing credential fails closed by name.** A connection serving verbs it
  has no credential for is a 401 the agent cannot read, so the field is named and
  the connection is refused.
* **A ``cli`` attachment has no way to receive one**, so a ``cli`` manifest that
  declares ``[[secrets]]`` is refused rather than silently prompting an operator
  for a value that goes nowhere — which is exactly the defect above.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent

from arcagent.connections import AuditChain, Connections
from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.attachment import ExtensionAttachment
from arcagent.extension.grants import ConnectionRegistry
from arcagent.extension.manifest import ExtensionManifest
from arcagent.extension.secrets import (
    LocalFileSecretBackend,
    Secret,
    SecretRef,
    SecretStore,
)
from arcagent.extension.state import ConnectionStateStore, open_connection_state
from arcagent.modules.connectors.install import (
    ConnectorPlan,
    build_attachment,
    connector_env_file,
    install_connector,
    plan_connector,
    resolve_secrets,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "reference_extension"

_BUNDLE = "reference_service"
_INSTANCE = "primary"
_AGENT = "credential_agent"
_CALLER = "did:arc:testorg:executor/credentials"
_FIELD = "reference_token"
_ECHO = "reference_echo"

#: A value nothing else in the system could produce, so finding it in a rendered
#: string proves it came out of the store rather than out of a formatter.
_TOKEN = "sentinel-7Qv3Xy-never-render-this"

#: What the fixture answers the fingerprint under, and the fingerprint itself.
#: Computed here independently of the fixture: a helper shared with the code under
#: test would agree with itself even when the credential never arrived.
_FINGERPRINT_KEY = "reference_token_fingerprint"
_FINGERPRINT = hashlib.sha256(_TOKEN.encode("utf-8")).hexdigest()[:16]


class _RecordingSink:
    """A real audit sink that keeps events. ``write`` only — the arctrust protocol."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def rendered(self) -> str:
        """Every event as one string — what a compliance reader would actually see."""
        return "\n".join(
            f"{event.actor_did} {event.action} {event.target} {event.outcome} {event.extra}"
            for event in self.events
        )

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def backend() -> FakeBackend:
    """Keep every surface in one test on one in-memory operational plane."""
    return FakeBackend()


def _bundle_root(tmp_path: Path) -> Path:
    """Copy the reference bundle into an extensions root, as an install would."""
    root = tmp_path / "extensions"
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_FIXTURE_DIR, root / _BUNDLE, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def _arc_dir(tmp_path: Path) -> Path:
    """The deployment root: where this test's connections and credentials live."""
    root = tmp_path / "arc"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _store(arc_dir: Path, sink: _RecordingSink | None = None) -> SecretStore:
    """The store the shipped surfaces use: the deployment's owner-only file."""
    return SecretStore(LocalFileSecretBackend(connector_env_file(arc_dir)), sink=sink)


def _plan(root: Path) -> ConnectorPlan:
    return plan_connector(
        extensions_root=[root],
        extension=_BUNDLE,
        instance=_INSTANCE,
        tier=Tier.PERSONAL,
        audit_sink=_RecordingSink(),
    )


async def _open_fake(backend: FakeBackend) -> FakeBackend:
    return backend


async def _state(backend: FakeBackend) -> ConnectionStateStore:
    """The connection directory an install registers into — this test's own backend."""
    return await open_connection_state(opener=lambda: _open_fake(backend))


async def _stored(arc_dir: Path, value: str = _TOKEN) -> SecretStore:
    """A store already holding the credential an operator supplied."""
    store = _store(arc_dir)
    await store.put(SecretRef(connection=_INSTANCE, field=_FIELD), value, caller_did=_CALLER)
    return store


def _connections(
    tmp_path: Path, root: Path, sink: _RecordingSink, backend: FakeBackend
) -> Connections:
    """The façade every surface drives, pointed entirely inside the test's own tree."""
    return Connections.for_deployment(
        arc_dir=_arc_dir(tmp_path),
        data_dir=tmp_path / "data",
        extensions_root=root,
        audit=AuditChain.held(sink),
        state_opener=lambda: _open_fake(backend),
    )


# --- the credential arrives ---------------------------------------------------


async def test_a_native_attachment_receives_its_declared_secrets_from_the_store(
    tmp_path: Path, backend: FakeBackend
) -> None:
    """The defect, inverted: the extension's own code must hold what the store holds.

    Built the way a running agent builds it — resolve from the store, then hand the
    resolved mapping to the shipped builder — and then asked, through the
    extension's own verb, for a fingerprint of what it received. A fingerprint
    rather than the value, because the value must never appear in a tool result;
    matching it proves the exact stored credential arrived, not merely that
    something did.
    """
    arc_dir = _arc_dir(tmp_path)
    root = _bundle_root(tmp_path)
    store = await _stored(arc_dir)
    plan = _plan(root)

    secrets = await resolve_secrets(
        plan.manifest, connection=_INSTANCE, store=store, caller_did=_CALLER
    )
    attachment = build_attachment(plan.manifest, plan.bundle, secrets)
    answer = await attachment.invoke(_ECHO, {"message": _FINGERPRINT_KEY})

    assert answer.content == f"reference echo: {_FINGERPRINT}", (
        "the extension did not receive the credential the operator stored: "
        "the attachment context reaches the extension's factory unmodified, so a "
        "mismatch here means the store was never read into it"
    )


async def test_the_install_path_hands_the_stored_credential_to_the_attachment_it_probes(
    tmp_path: Path, backend: FakeBackend
) -> None:
    """``arc connector add`` must probe a CONFIGURED connection, not a blank one.

    The factory wraps the shipped builder rather than replacing it, so the probe
    runs against the real attachment and the assertion still sees what the install
    resolved. A probe against an attachment holding no credential is the failure an
    operator actually hit: three credentials supplied, ``jira is not configured``.
    """
    arc_dir = _arc_dir(tmp_path)
    root = _bundle_root(tmp_path)
    handed: list[dict[str, str]] = []

    def _recording(
        manifest: ExtensionManifest, bundle: Path, secrets: Mapping[str, Secret]
    ) -> ExtensionAttachment:
        handed.append({name: secret.reveal() for name, secret in secrets.items()})
        return build_attachment(manifest, bundle, secrets)

    report = await install_connector(
        _plan(root),
        connections=ConnectionRegistry(arc_dir),
        agents=[_AGENT],
        secret_values={_FIELD: _TOKEN},
        store=_store(arc_dir),
        caller_did=_CALLER,
        state=await _state(backend),
        attachment_factory=_recording,
    )

    assert handed == [{_FIELD: _TOKEN}]
    assert "authenticated" in report.detail
    assert "unauthenticated" not in report.detail


async def test_the_facade_probes_a_connection_that_holds_its_credential(
    tmp_path: Path, backend: FakeBackend
) -> None:
    """Site one: every read verb a surface offers builds a CONFIGURED attachment.

    ``probe``, ``tools``, ``doctor`` and ``approve`` all go through one builder in
    :class:`~arcagent.connections.Connections`. Probing is the only honest answer to
    "does this work", so a probe that answers from a credential-less attachment is a
    surface reporting on a connection nobody has.
    """
    arc_dir = _arc_dir(tmp_path)
    root = _bundle_root(tmp_path)
    sink = _RecordingSink()
    await install_connector(
        _plan(root),
        connections=ConnectionRegistry(arc_dir),
        agents=[_AGENT],
        secret_values={_FIELD: _TOKEN},
        store=_store(arc_dir),
        caller_did=_CALLER,
        state=await _state(backend),
    )

    result = await _connections(tmp_path, root, sink, backend).probe(_INSTANCE)

    assert result.reachable
    assert "authenticated" in result.detail
    assert "unauthenticated" not in result.detail


# --- and nothing renders it ---------------------------------------------------


async def test_a_revealed_credential_never_reaches_a_rendered_string(
    tmp_path: Path, backend: FakeBackend
) -> None:
    """LLM02/LLM07 — a ``Secret`` is unwrapped at the factory boundary and nowhere else.

    Asserted on the rendered strings a surface can actually show an operator or put
    in a prompt, with a sentinel value: the install report, the tool list, every
    audit event the chain recorded, and the doctor rows that report a credential's
    presence. ``reveal()`` is the one call that ends the ``Secret(***)`` protection,
    so a second call site anywhere upstream shows up here.
    """
    arc_dir = _arc_dir(tmp_path)
    root = _bundle_root(tmp_path)
    sink = _RecordingSink()
    report = await install_connector(
        _plan(root),
        connections=ConnectionRegistry(arc_dir),
        agents=[_AGENT],
        secret_values={_FIELD: _TOKEN},
        store=_store(arc_dir, sink),
        caller_did=_CALLER,
        state=await _state(backend),
        audit_sink=sink,
    )
    connections = _connections(tmp_path, root, sink, backend)

    specs = await connections.tools(_INSTANCE)
    checks = await connections.doctor(_INSTANCE)

    rendered = [
        report.detail,
        str(report),
        str(specs),
        str(checks),
        sink.rendered(),
    ]
    for text in rendered:
        assert _TOKEN not in text, f"a revealed credential was rendered into: {text}"
    assert any(check.status == "present" for check in checks), "doctor saw no credential at all"


async def test_a_refusal_names_the_missing_credential_and_never_its_value(
    tmp_path: Path,
) -> None:
    """Fail closed, by name. A field the store does not hold stops the connection.

    The instance is configured and its credential is gone — a rotation that was
    deleted, a vault that lost it — which is the one state where a connection would
    otherwise attach and serve verbs that answer 401.
    """
    arc_dir = _arc_dir(tmp_path)
    root = _bundle_root(tmp_path)
    store = _store(arc_dir)

    with pytest.raises(ExtensionError) as caught:
        await resolve_secrets(
            _plan(root).manifest, connection=_INSTANCE, store=store, caller_did=_CALLER
        )

    error = caught.value
    assert error.details["missing"] == [_FIELD]
    assert _FIELD in error.message
    assert _INSTANCE in error.message
    assert _TOKEN not in error.message


async def test_a_bundle_declaring_no_credential_needs_no_store(tmp_path: Path) -> None:
    """A ``cli`` bundle whose binary owns its own auth must attach with no store at all.

    Every shipped ``cli`` bundle is this shape: ``gh auth login`` keeps the token in
    the host keyring and Arc never handles it. Refusing those because a deployment
    configured no secret store would take the safest connectors away first.
    """
    manifest = _cli_manifest(secrets=False)

    resolved = await resolve_secrets(
        manifest, connection=_INSTANCE, store=None, caller_did=_CALLER
    )

    assert resolved == {}
    assert build_attachment(manifest, tmp_path, resolved) is not None


def test_a_cli_manifest_that_declares_a_credential_is_refused(tmp_path: Path) -> None:
    """A ``cli`` attachment spawns a binary; there is no declared way to hand it a value.

    Prompting an operator for a credential that then goes nowhere is the defect this
    whole suite exists to close, so the incoherent manifest is refused by name rather
    than accepted and silently dropped.
    """
    manifest = _cli_manifest(secrets=True)

    with pytest.raises(ExtensionError) as caught:
        build_attachment(manifest, tmp_path, {})

    assert "api_token" in caught.value.message


def _cli_manifest(*, secrets: bool) -> ExtensionManifest:
    """A minimal ``cli`` manifest, parsed by the shipped parser."""
    from arcagent.extension.manifest import load_manifest

    declared = '\n[[secrets]]\nname = "api_token"\n' if secrets else ""
    return load_manifest(
        "[extension]\n"
        'name = "acme"\n'
        'version = "1.0.0"\n'
        'attachment = "cli"\n'
        "\n[tools]\n"
        "allow = []\n"
        "\n[config.cli]\n"
        'binary = "acme"\n' + declared,
        tier=Tier.PERSONAL,
    )


# --- an optional field ---------------------------------------------------------


def _with_optional(manifest: ExtensionManifest) -> ExtensionManifest:
    """The reference bundle plus one field the operator may leave blank."""
    extra = manifest.secrets[0].model_copy(
        update={"name": "host", "required": False, "sensitive": False}
    )
    return manifest.model_copy(update={"secrets": [*manifest.secrets, extra]})


async def test_an_optional_field_left_blank_still_connects(
    tmp_path: Path, backend: FakeBackend
) -> None:
    """An optional field's EMPTINESS is meaningful, so it cannot be a refusal.

    sqlite declares `host`, and leaving it blank is how an operator says the
    database is on this machine — the ordinary case. Requiring every declared
    field made that case unconnectable: the install refused with "no value
    supplied for required secret 'host'" and stored nothing.
    """
    arc_dir = _arc_dir(tmp_path)
    root = _bundle_root(tmp_path)
    store = await _stored(arc_dir)
    manifest = _with_optional(_plan(root).manifest)

    secrets = await resolve_secrets(
        manifest, connection=_INSTANCE, store=store, caller_did=_CALLER
    )

    assert _FIELD in secrets
    assert "host" not in secrets, "an absent optional field must not become an empty one"


async def test_a_required_field_left_blank_is_still_refused_by_name(
    tmp_path: Path, backend: FakeBackend
) -> None:
    """The opt-out must not weaken the guard for everything else."""
    root = _bundle_root(tmp_path)
    store = _store(_arc_dir(tmp_path))

    with pytest.raises(ExtensionError) as caught:
        await resolve_secrets(
            _plan(root).manifest, connection=_INSTANCE, store=store, caller_did=_CALLER
        )

    assert _FIELD in caught.value.message
