"""Vault KV v2 monotonic-head contract and abuse regressions."""

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from arctrust import AnchorHead, AnchorUnavailableError
from arctrust.vault_anchor import VaultKVAnchor


class FakeVault:
    def __init__(self) -> None:
        self.version = 0
        self.digest = ""
        self.previous_digest: str | None = None
        self.intent = ""
        self.scope = "queue/prod/head"
        self.cas_required = True
        self.mount_cas_required = True
        self.fail_write = False
        self.raise_after_write = False
        self.reset = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.reset:
            return httpx.Response(404)
        if request.method == "GET" and "/metadata/" in request.url.path:
            if not self.version:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "data": {"cas_required": self.cas_required, "current_version": self.version}
                },
            )
        if request.method == "GET" and request.url.path.endswith("/config"):
            return httpx.Response(200, json={"data": {"cas_required": self.mount_cas_required}})
        if request.method == "GET" and "/data/" in request.url.path:
            if not self.version:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "data": {
                            "digest": self.digest,
                            "scope": self.scope,
                            "previous_digest": self.previous_digest,
                            "intent": self.intent,
                        },
                        "metadata": {
                            "version": self.version,
                            "deletion_time": "",
                            "destroyed": False,
                        },
                    }
                },
            )
        if request.method == "POST" and "/data/" in request.url.path:
            if self.fail_write:
                return httpx.Response(503)
            import json

            payload = json.loads(request.content)
            if payload["options"]["cas"] != self.version:
                return httpx.Response(400)
            self.version += 1
            self.digest = payload["data"]["digest"]
            self.previous_digest = payload["data"]["previous_digest"]
            self.intent = payload["data"]["intent"]
            self.scope = payload["data"]["scope"]
            if self.raise_after_write:
                raise httpx.ReadTimeout("response lost after write")
            return httpx.Response(200, json={"data": {"version": self.version}})
        return httpx.Response(404)


class _Bootstrap:
    def __init__(self) -> None:
        self.used = False

    def consume(self, scope: str) -> bool:
        if scope != "queue/prod/head" or self.used:
            return False
        self.used = True
        return True


def _anchor(vault: FakeVault, *, bootstrap: bool = True) -> VaultKVAnchor:
    client = httpx.Client(base_url="https://vault.example", transport=httpx.MockTransport(vault))
    return VaultKVAnchor(
        client,
        mount="queue",
        record="prod/head",
        bootstrap_authority=_Bootstrap() if bootstrap else None,
    )


def test_vault_anchor_advances_with_strict_cas_and_rejects_stale_owner() -> None:
    vault = FakeVault()
    anchor = _anchor(vault)
    first = anchor.compare_and_advance(None, "a" * 64, "")
    assert first == AnchorHead(scope="queue/prod/head", version=1, digest="a" * 64)
    second = anchor.compare_and_advance(first, "b" * 64, "ciphertext")
    assert second.version == 2
    with pytest.raises(AnchorUnavailableError, match="stale"):
        anchor.compare_and_advance(first, "c" * 64, "ciphertext")


def test_vault_anchor_fails_closed_on_reset_missing_and_cas_downgrade() -> None:
    vault = FakeVault()
    anchor = _anchor(vault)
    first = anchor.compare_and_advance(None, "a" * 64, "")
    vault.version = 0
    with pytest.raises(AnchorUnavailableError, match="missing or reset"):
        anchor.latest()
    vault.version = first.version
    vault.cas_required = False
    vault.mount_cas_required = False
    with pytest.raises(AnchorUnavailableError, match="CAS"):
        anchor.latest()


def test_vault_anchor_rejects_unavailable_write_and_insecure_configuration() -> None:
    vault = FakeVault()
    anchor = _anchor(vault)
    vault.fail_write = True
    with pytest.raises(AnchorUnavailableError, match="refused"):
        anchor.compare_and_advance(None, "a" * 64, "")
    with pytest.raises(ValueError, match="HTTPS"):
        VaultKVAnchor(httpx.Client(base_url="http://vault.example"), mount="queue", record="head")
    with pytest.raises(ValueError, match="path"):
        VaultKVAnchor(
            httpx.Client(base_url="https://vault.example"), mount="queue", record="../head"
        )


def test_vault_anchor_requires_explicit_bootstrap() -> None:
    with pytest.raises(AnchorUnavailableError, match="missing or reset"):
        _anchor(FakeVault(), bootstrap=False).latest()


def test_mount_wide_cas_satisfies_requirement_when_per_key_flag_is_false() -> None:
    vault = FakeVault()
    anchor = _anchor(vault)
    anchor.compare_and_advance(None, "a" * 64, "")
    vault.cas_required = False
    assert anchor.latest().version == 1
    vault.mount_cas_required = False
    with pytest.raises(AnchorUnavailableError, match="CAS"):
        anchor.latest()


def test_vault_anchor_recognizes_success_after_lost_response_without_rewrite() -> None:
    vault = FakeVault()
    anchor = _anchor(vault)
    vault.raise_after_write = True
    head = anchor.compare_and_advance(None, "a" * 64, "sealed-intent")
    assert head.version == vault.version == 1
    assert head.intent == "sealed-intent"


def test_vault_anchor_rejects_same_revision_content_or_scope_substitution() -> None:
    vault = FakeVault()
    anchor = _anchor(vault)
    anchor.compare_and_advance(None, "a" * 64, "")
    vault.digest = "b" * 64
    with pytest.raises(AnchorUnavailableError, match="invalid"):
        anchor.latest()
    vault.digest = "a" * 64
    vault.scope = "other/head"
    with pytest.raises(AnchorUnavailableError, match="invalid"):
        anchor.latest()


def test_concurrent_latest_and_cas_never_regress_seen_head() -> None:
    anchor = _anchor(FakeVault())
    first = anchor.compare_and_advance(None, "a" * 64, "")

    def advance(index: int) -> AnchorHead | None:
        try:
            return anchor.compare_and_advance(first, f"{index:064x}", "intent")
        except AnchorUnavailableError:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(advance, range(1, 9)))
        observed = list(executor.map(lambda _: anchor.latest(), range(8)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert all(head == winners[0] for head in observed)
