"""Scoped queue record envelopes over nonexportable authenticated byte custody."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arctrust.byte_cipher import ByteCipher
from arctrust.canonical import canonical_json

_PREFIX = b"arc:record-envelope:v1\0"
_SEALED_KEY = "arc.audit.sealed"
_MAX_PLAINTEXT = 512 * 1024


class _Envelope(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    version: Literal[1] = 1
    tenant_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    journal_scope: str = Field(pattern=r"^[a-zA-Z0-9_/-]{1,256}$")
    purpose: str = Field(pattern=r"^[a-z][a-z0-9.]{1,63}$")
    content: dict[str, Any]


class VaultRecordCipher:
    """Seal queue metadata with a fixed tenant, journal and purpose binding.

    A production factory supplies a scoped VaultCipher as the byte capability.
    This adapter never receives an exportable key or logs record content.
    """

    def __init__(
        self,
        cipher: ByteCipher,
        *,
        tenant_id: str,
        journal_scope: str,
        purpose: str,
    ) -> None:
        _Envelope(tenant_id=tenant_id, journal_scope=journal_scope, purpose=purpose, content={})
        self._cipher = cipher
        self._tenant_id = tenant_id
        self._scope = journal_scope
        self._purpose = purpose

    @property
    def tenant_id(self) -> str:
        """Return the immutable authenticated tenant binding."""
        return self._tenant_id

    @property
    def journal_scope(self) -> str:
        """Return the immutable authenticated journal binding."""
        return self._scope

    @property
    def purpose(self) -> str:
        """Return the immutable authenticated record purpose."""
        return self._purpose

    def seal(self, event: dict[str, Any]) -> dict[str, Any]:
        """Return one versioned Vault ciphertext in the canonical extra slot."""
        content = event.get("extra")
        if not isinstance(content, dict):
            raise ValueError("record envelope content must be an object")
        envelope = _Envelope(
            tenant_id=self._tenant_id,
            journal_scope=self._scope,
            purpose=self._purpose,
            content=content,
        )
        payload = _PREFIX + canonical_json(envelope.model_dump(mode="json"))
        if len(payload) > _MAX_PLAINTEXT:
            raise ValueError("record envelope exceeds Vault limit")
        return {**event, "extra": {_SEALED_KEY: self._cipher.seal(payload)}}

    def unseal(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Open only ciphertext authenticated for this exact queue scope."""
        sealed = event.get("extra")
        if not isinstance(sealed, dict) or set(sealed) != {_SEALED_KEY}:
            raise ValueError("record envelope is not sealed")
        ciphertext = sealed[_SEALED_KEY]
        if not isinstance(ciphertext, str) or len(ciphertext) > 1_048_576:
            raise ValueError("record envelope ciphertext invalid")
        payload = self._cipher.open(ciphertext)
        if not payload.startswith(_PREFIX) or len(payload) > len(_PREFIX) + _MAX_PLAINTEXT:
            raise ValueError("record envelope domain invalid")
        raw = payload[len(_PREFIX) :]
        try:
            decoded = json.loads(raw)
            if canonical_json(decoded) != raw:
                raise ValueError("record envelope is not canonical")
            envelope = _Envelope.model_validate(decoded)
        except (ValidationError, ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("record envelope payload invalid") from exc
        if (
            envelope.tenant_id != self._tenant_id
            or envelope.journal_scope != self._scope
            or envelope.purpose != self._purpose
        ):
            raise ValueError("record envelope scope mismatch")
        return {**event, "extra": envelope.content}
