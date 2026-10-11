"""SPEC-088 T-1369 (RED): the dedicated work-events signing key (COMP-019, REQ-626, D-848).

Contract pinned here for the builder (T-1370), new module ``arctrust.work_event_signer``
(modeled on :mod:`arctrust.knowledge_signer`):

- ``WORK_EVENTS_KEY_REF = "work-events"`` — the custody key reference.
- ``provision_work_event_key(security, *, base=None)`` mints that key in the deployment's
  custody transit (the local notary keystore when no ``[security.vault]`` is configured,
  Vault Transit otherwise) as a non-exportable key with the deployment
  ``signing_algorithm``. On Vault it sends ``POST /v1/<mount>/keys/<vault name>`` with
  ``exportable = false`` and ``allow_plaintext_backup = false`` set EXPLICITLY (the flags
  ``transit_http.create_user_key`` sends). Idempotent: a second call never replaces an
  existing key (the public key is unchanged).
- ``work_event_signer_for(security, *, base=None) -> WorkEventSigner | None``: a
  sign-by-reference capability whose public surface is exactly ``sign``, ``public_key``,
  ``algorithm`` and ``key_id`` (``hashlib.sha256(public_key).hexdigest()[:16]``) and holds no
  seed/private accessor. An unprovisioned key answers ``None``. It NEVER falls back to the
  operator key, to a key derived from the operator seed (``knowledge_signer``'s
  in-process path), or to any in-process seed.

``security`` is the duck-typed ``MachineSecurity`` view every arctrust resolver takes.
External boundaries faked: none for the notary (it is the real out-of-process notary);
Vault is the strict real-TLS ``FakeVault`` (which records every key-creation request).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from packages.arctrust.tests.vault_fake import FakeVault

from arctrust import FileNotaryTransit, OperatorKey, machine_security, verify_signature
from arctrust import operator_resolver as resolver
from arctrust.operator_resolver import OPERATOR_KEY_REF, MachineSecurity
from arctrust.paths import config_file, default_operator_key_path

_VAULT_NAME = "arc-work-events"
_PUBLIC_SURFACE = {"algorithm", "key_id", "public_key", "sign"}
_PRIVATE_WORDS = ("seed", "private", "secret", "export")


def _module() -> Any:
    from arctrust import work_event_signer

    return work_event_signer


# --- custody fixtures -------------------------------------------------------------


def _notary(tmp_path: Path, algorithm: str = "ed25519") -> MachineSecurity:
    """A local out-of-process notary that already serves the operator key."""
    keystore = tmp_path / "notary"
    FileNotaryTransit.provision(keystore, OPERATOR_KEY_REF, os.urandom(32), algorithm)
    return MachineSecurity(
        custody="vault_transit", signing_algorithm=algorithm, notary_keystore=str(keystore)
    )


def _operator_public(security: MachineSecurity) -> bytes:
    transit = FileNotaryTransit(Path(security.notary_keystore), security.signing_algorithm)
    return transit.public_key(OPERATOR_KEY_REF)


def _personal(tmp_path: Path) -> MachineSecurity:
    """Personal tier, zero config: in-process custody with an on-disk operator key."""
    security = MachineSecurity(operator_key_dir=str(tmp_path / "keys"))
    resolver.operator_key_for(security, bootstrap=True)
    return security


def _public_attrs(obj: object) -> set[str]:
    return {name for name in dir(obj) if not name.startswith("_")}


# --- constants and shape ------------------------------------------------------------


def test_work_events_key_ref_is_work_events() -> None:
    assert _module().WORK_EVENTS_KEY_REF == "work-events"


def test_unprovisioned_notary_key_returns_none(tmp_path: Path) -> None:
    security = _notary(tmp_path)

    assert _module().work_event_signer_for(security) is None


def test_unprovisioned_personal_key_returns_none_not_an_operator_derived_key(
    tmp_path: Path,
) -> None:
    """Unlike knowledge_signer, nothing is derived from the operator seed: fail closed."""
    security = _personal(tmp_path)

    assert _module().work_event_signer_for(security) is None


# --- notary provisioning ------------------------------------------------------------


def test_provision_on_notary_creates_a_key_distinct_from_the_operator_key(
    tmp_path: Path,
) -> None:
    mod = _module()
    security = _notary(tmp_path)

    mod.provision_work_event_key(security)
    signer = mod.work_event_signer_for(security)

    assert signer is not None
    assert signer.public_key != _operator_public(security)
    keystore = FileNotaryTransit(Path(security.notary_keystore))
    assert keystore.public_key(mod.WORK_EVENTS_KEY_REF) == signer.public_key
    message = b"arc.work-event.v1\n1760097600\nevt-1\n" + b"0" * 64
    signature = signer.sign(message)
    assert verify_signature(signer.algorithm, message, signature, signer.public_key)
    assert not verify_signature(signer.algorithm, message, signature, _operator_public(security))


def test_second_provision_never_overwrites_the_existing_key(tmp_path: Path) -> None:
    mod = _module()
    security = _notary(tmp_path)

    mod.provision_work_event_key(security)
    first = mod.work_event_signer_for(security)
    assert first is not None
    try:
        mod.provision_work_event_key(security)
    except Exception as exc:  # an explicit refusal is allowed; replacement is not
        assert "work-events" in str(exc) or "exist" in str(exc).lower()
    again = mod.work_event_signer_for(security)

    assert again is not None
    assert again.public_key == first.public_key, "a second provision replaced the key"
    signature = first.sign(b"still the same key")
    assert verify_signature(again.algorithm, b"still the same key", signature, again.public_key)


def test_provision_uses_the_deployment_algorithm_on_a_fips_notary(tmp_path: Path) -> None:
    mod = _module()
    security = _notary(tmp_path, algorithm="ecdsa-p256")

    mod.provision_work_event_key(security)
    signer = mod.work_event_signer_for(security)

    assert signer is not None
    assert signer.algorithm == "ecdsa-p256"
    signature = signer.sign(b"fips")
    assert verify_signature("ecdsa-p256", b"fips", signature, signer.public_key)
    assert signer.public_key != _operator_public(security)


def test_personal_tier_provision_gives_a_signer_that_is_not_the_operator_key(
    tmp_path: Path,
) -> None:
    """``arc init`` at personal tier must leave callbacks signable, never by the operator."""
    mod = _module()
    security = _personal(tmp_path)
    operator = resolver.operator_key_for(security)
    assert operator is not None

    mod.provision_work_event_key(security)
    signer = mod.work_event_signer_for(security)

    assert signer is not None
    assert signer.public_key != operator.public_key
    signature = signer.sign(b"personal")
    assert verify_signature(signer.algorithm, b"personal", signature, signer.public_key)
    assert not verify_signature(signer.algorithm, b"personal", signature, operator.public_key)


# --- the capability exposes no private material -------------------------------------


def test_capability_public_surface_is_sign_public_key_algorithm_key_id(tmp_path: Path) -> None:
    mod = _module()
    security = _notary(tmp_path)
    mod.provision_work_event_key(security)

    signer = mod.work_event_signer_for(security)

    assert signer is not None
    assert _public_attrs(signer) == _PUBLIC_SURFACE
    leaked = [name for name in dir(signer) if any(word in name.lower() for word in _PRIVATE_WORDS)]
    assert leaked == [], f"capability exposes private-looking attributes: {leaked}"
    assert isinstance(signer.public_key, bytes)


def test_key_id_is_sha256_of_public_key_first_16_hex(tmp_path: Path) -> None:
    mod = _module()
    security = _notary(tmp_path)
    mod.provision_work_event_key(security)

    signer = mod.work_event_signer_for(security)

    assert signer is not None
    assert signer.key_id == hashlib.sha256(signer.public_key).hexdigest()[:16]
    assert signer.algorithm == "ed25519"


def test_signing_does_not_read_the_seed_into_this_process(tmp_path: Path) -> None:
    """The seed stays in the notary keystore; the capability holds no copy of it.

    Walks every attribute reachable from the capability (slots and dicts) and refuses
    any bytes equal to the seed: an in-process signer built from the seed would hold it.
    """
    mod = _module()
    security = _notary(tmp_path)
    mod.provision_work_event_key(security)
    seed = Path(security.notary_keystore) / f"{mod.WORK_EVENTS_KEY_REF}.seed"
    assert seed.exists(), "the notary keystore holds the work-events seed (out of process)"

    signer = mod.work_event_signer_for(security)

    assert signer is not None
    assert not any(
        isinstance(value, (bytes, bytearray)) and value == seed.read_bytes()
        for value in _reachable_values(signer)
    ), "the seed bytes are held by the in-process capability"


def _reachable_values(obj: object, depth: int = 3) -> Iterator[object]:
    """Every attribute value reachable from ``obj`` (slots and dicts), a few levels deep."""
    seen: set[int] = set()
    frontier: list[tuple[object, int]] = [(obj, 0)]
    while frontier:
        current, level = frontier.pop()
        if id(current) in seen or level > depth:
            continue
        seen.add(id(current))
        yield current
        names: list[str] = list(getattr(current, "__dict__", {}) or {})
        for klass in type(current).__mro__:
            names.extend(getattr(klass, "__slots__", ()))
        for name in names:
            try:
                value = object.__getattribute__(current, name)
            except AttributeError:
                continue
            if isinstance(value, (str, int, float, bool, type(None))):
                continue
            frontier.append((value, level + 1))


# --- Vault Transit -------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeVault]:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
    with FakeVault() as fake:
        creds = tmp_path / "creds"
        creds.mkdir()
        (creds / "vault-secret-id").write_text(fake.state.secret_id)
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
        path = config_file("arcagent.toml")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[security]\n"
            "[security.vault]\n"
            f'addr = "{fake.addr}"\n'
            f'ca_bundle = "{fake.ca_bundle}"\n'
            f'role_id = "{fake.state.role_id}"\n'
            'secret_source = "credential:vault-secret-id"\n'
            "max_retries = 0\n"
            "[security.vault.keys]\n"
            'operator = "arc-operator"\n'
            'connector-credentials = "arc-connector-credentials"\n'
            f'work-events = "{_VAULT_NAME}"\n',
            encoding="utf-8",
        )
        yield fake
    resolver._VAULT_TRANSITS.clear()


def test_vault_provision_requests_a_non_exportable_key(vault: FakeVault) -> None:
    mod = _module()
    security = machine_security()

    mod.provision_work_event_key(security)

    requests = [body for name, body in vault.state.created if name == _VAULT_NAME]
    assert len(requests) == 1, vault.state.created
    (body,) = requests
    assert body.get("exportable") is False
    assert body.get("allow_plaintext_backup") is False
    assert body.get("type") == "ed25519"
    assert all(name != "arc-operator" for name, _ in vault.state.created), (
        "provisioning touched the operator key"
    )


def test_vault_signer_signs_by_reference_with_the_work_events_key(vault: FakeVault) -> None:
    mod = _module()
    security = machine_security()
    mod.provision_work_event_key(security)

    signer = mod.work_event_signer_for(security)

    assert signer is not None
    assert signer.public_key == bytes(vault.state.keys[_VAULT_NAME].material.verify_key)
    assert signer.public_key != bytes(vault.state.keys["arc-operator"].material.verify_key)
    signature = signer.sign(b"callback")
    assert verify_signature(signer.algorithm, b"callback", signature, signer.public_key)
    assert vault.calls(f"/sign/{_VAULT_NAME}") == 1
    assert vault.calls("/sign/arc-operator") == 0


def test_vault_second_provision_leaves_the_key_unchanged(vault: FakeVault) -> None:
    mod = _module()
    security = machine_security()
    mod.provision_work_event_key(security)
    before = bytes(vault.state.keys[_VAULT_NAME].material.verify_key)

    try:
        mod.provision_work_event_key(security)
    except Exception as exc:  # an explicit refusal is allowed; replacement is not
        assert "work-events" in str(exc) or "exist" in str(exc).lower()

    assert bytes(vault.state.keys[_VAULT_NAME].material.verify_key) == before
    signer = mod.work_event_signer_for(security)
    assert signer is not None and signer.public_key == before


def test_vault_unprovisioned_key_returns_none_and_never_signs_with_the_operator(
    vault: FakeVault,
) -> None:
    mod = _module()

    signer = mod.work_event_signer_for(machine_security())

    assert signer is None
    assert vault.calls("/sign/arc-operator") == 0


def test_vault_custody_never_falls_back_to_an_on_disk_operator_key(vault: FakeVault) -> None:
    """An operator key file on the box must not stand in for an absent work-events key."""
    OperatorKey.load(default_operator_key_path(), generate_if_absent=True)

    assert _module().work_event_signer_for(machine_security()) is None
