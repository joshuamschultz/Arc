"""Vault KV v2 implementation of an externally custodied monotonic head."""

from __future__ import annotations

import re
import threading

import httpx

from arctrust.monotonic import AnchorHead, AnchorUnavailableError, BootstrapAuthority

_PATH_RE = re.compile(r"^[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class _SnapshotChangedError(Exception):
    pass


class VaultKVAnchor:
    """CAS head in Vault KV v2 under a separately controlled policy.

    ``client`` is an injected, already authenticated HTTPS capability. This
    leaf never reads or stores a reusable token. Its policy must deny KV data
    deletion, version destruction, metadata deletion/reset, and mount reconfig.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        mount: str,
        record: str,
        bootstrap_authority: BootstrapAuthority | None = None,
    ) -> None:
        if client.base_url.scheme != "https":
            raise ValueError("Vault anchor requires HTTPS")
        if not _PATH_RE.fullmatch(mount) or not _PATH_RE.fullmatch(record):
            raise ValueError("invalid Vault KV anchor path")
        self._client = client
        self._data_path = f"/v1/{mount}/data/{record}"
        self._metadata_path = f"/v1/{mount}/metadata/{record}"
        self._config_path = f"/v1/{mount}/config"
        self._scope = f"{mount}/{record}"
        self._bootstrap_authority = bootstrap_authority
        self._seen_version = 0
        self._seen_head: AnchorHead | None = None
        self._lock = threading.RLock()

    @property
    def scope(self) -> str:
        """Stable namespace bound into every witnessed revision."""
        return self._scope

    def _get(self, path: str) -> httpx.Response:
        try:
            response = self._client.get(path, timeout=5.0, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise AnchorUnavailableError("Vault anchor read unavailable") from exc
        if response.status_code not in {200, 404}:
            raise AnchorUnavailableError("Vault anchor read refused")
        return response

    def latest(self) -> AnchorHead | None:
        """Read and cross-check KV current version, CAS posture, and digest."""
        with self._lock:
            for _ in range(3):
                try:
                    return self._latest_once()
                except _SnapshotChangedError:
                    continue
        raise AnchorUnavailableError("Vault anchor snapshot changed during read")

    def _latest_once(self) -> AnchorHead | None:
        metadata = self._get(self._metadata_path)
        if metadata.status_code == 404:
            if self._seen_version or self._bootstrap_authority is None:
                raise AnchorUnavailableError("Vault anchor missing or reset")
            self._require_cas(None)
            return None
        try:
            detail = metadata.json()["data"]
            version = detail["current_version"]
            if type(version) is not int or version < 1:
                raise ValueError
            self._require_cas(detail.get("cas_required"))
            if version < self._seen_version:
                raise ValueError
            data = self._get(self._data_path)
            if data.status_code != 200:
                raise ValueError
            envelope = data.json()["data"]
            revision = envelope["metadata"]
            if revision["version"] != version:
                raise _SnapshotChangedError
            if revision["deletion_time"] != "" or revision["destroyed"] is not False:
                raise ValueError
            state = envelope["data"]
            head = AnchorHead(
                scope=state["scope"],
                version=version,
                digest=state["digest"],
                previous_digest=state.get("previous_digest"),
                intent=state.get("intent", ""),
            )
            if head.scope != self._scope:
                raise ValueError
            if self._seen_head is not None and head.version == self._seen_head.version:
                if head != self._seen_head:
                    raise ValueError
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise AnchorUnavailableError("Vault anchor state is invalid") from exc
        self._seen_version = head.version
        self._seen_head = head
        return head

    def _require_cas(self, per_key: object) -> None:
        if per_key is True:
            return
        config = self._get(self._config_path)
        try:
            if config.status_code != 200 or config.json()["data"]["cas_required"] is not True:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise AnchorUnavailableError("Vault KV CAS is not mandatory") from exc

    def compare_and_advance(
        self, expected: AnchorHead | None, digest: str, intent: str
    ) -> AnchorHead:
        """Write one new head with Vault CAS and verify the resulting revision."""
        with self._lock:
            return self._compare_unlocked(expected, digest, intent)

    def _compare_unlocked(
        self, expected: AnchorHead | None, digest: str, intent: str
    ) -> AnchorHead:
        if type(digest) is not str or not _DIGEST_RE.fullmatch(digest):
            raise ValueError("invalid anchor digest")
        if type(intent) is not str or len(intent) > 1_048_576:
            raise ValueError("invalid anchor intent")
        current = self.latest()
        if current != expected:
            raise AnchorUnavailableError("Vault anchor CAS owner is stale")
        if current is None:
            authority = self._bootstrap_authority
            if authority is None or not authority.consume(self._scope):
                raise AnchorUnavailableError("Vault anchor bootstrap not authorized")
        cas = 0 if current is None else current.version
        try:
            response = self._client.post(
                self._data_path,
                json={
                    "options": {"cas": cas},
                    "data": {
                        "digest": digest,
                        "scope": self._scope,
                        "previous_digest": current.digest if current else None,
                        "intent": intent,
                    },
                },
                timeout=5.0,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            response = None
            failure: Exception | None = exc
        else:
            failure = None
        expected_head = AnchorHead(
            scope=self._scope,
            version=cas + 1,
            digest=digest,
            previous_digest=current.digest if current else None,
            intent=intent,
        )
        try:
            observed = self.latest()
        except AnchorUnavailableError as exc:
            raise AnchorUnavailableError("Vault anchor write outcome is uncertain") from exc
        if observed == expected_head:
            return observed
        if response is None or response.status_code != 200:
            raise AnchorUnavailableError("Vault anchor CAS write refused") from failure
        raise AnchorUnavailableError("Vault anchor write verification failed")
