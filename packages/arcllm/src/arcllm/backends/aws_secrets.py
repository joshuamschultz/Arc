"""AWS Secrets Manager vault backend for arcllm.

Implements the arcllm.vault.VaultBackend protocol using boto3. Credential
discovery follows boto3's default chain (IAM Instance Profile → ~/.aws/
credentials → AWS_ACCESS_KEY_ID env vars). The class never accepts a raw
key/secret pair — pinning credentials in code defeats the IAM-Instance-
Profile design that justifies this backend's existence on EC2/Lightsail.

Error semantics (intentional, see vault.py for the resolver side):

    ResourceNotFoundException       → return None  (caller may try env)
    AccessDeniedException           → raise ArcLLMConfigError  (fail loud)
    NoCredentialsError / network    → return None + is_available()=False
    boto3 not installed             → is_available()=False, no raise

Retries and TTL caching live in VaultResolver. This class fetches once per
call, decodes the response, and either returns or signals unavailable.
"""

from __future__ import annotations

import logging
from typing import Any

from arcllm.exceptions import ArcLLMConfigError

logger = logging.getLogger("arcllm.backends.aws_secrets")

# AWS error codes we treat specially. Anything else falls through to the
# generic "service degraded" branch and marks the backend unavailable.
_NOT_FOUND_CODES = frozenset({"ResourceNotFoundException"})
_ACCESS_DENIED_CODES = frozenset(
    {"AccessDeniedException", "UnauthorizedOperation", "InvalidSignatureException"}
)


class AwsSecretsManagerBackend:
    """VaultBackend backed by AWS Secrets Manager.

    The boto3 client is created once in ``__init__`` and reused (boto3
    clients are thread-safe). If boto3 isn't installed or credentials
    can't be resolved, the backend reports ``is_available()=False`` so
    VaultResolver falls back to its env-var path.
    """

    def __init__(
        self,
        *,
        region_name: str | None = None,
        profile_name: str | None = None,
    ) -> None:
        # When region_name is None, boto3 falls through its natural chain:
        # AWS_REGION env -> AWS_DEFAULT_REGION env -> ~/.aws/config -> instance
        # metadata. Hardcoding a default here would block that chain — the
        # operator could not override per-deployment. SPEC-025 review fix.
        self._region_name = region_name
        self._profile_name = profile_name
        self._client: Any | None = None
        self._available: bool = False
        self._client_errors: tuple[type[Exception], ...] = ()
        self._not_found_errors: tuple[type[Exception], ...] = ()
        self._build_client()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_client(self) -> None:
        """Lazy-import boto3 + construct the SecretsManager client.

        On any import or construction failure, leave ``_client=None`` and
        ``_available=False``. Callers see this via is_available() and
        VaultResolver falls back to env vars.
        """
        try:
            # Lazy-import: a deployment that doesn't use this backend pays
            # nothing at module-import time. boto3 stubs aren't a project
            # dep, so type-checking treats this as untyped — that's fine,
            # we only call two well-documented methods.
            import boto3  # type: ignore[import-untyped]
            from botocore.exceptions import (  # type: ignore[import-untyped]
                BotoCoreError,
                ClientError,
            )
        except ImportError:
            logger.warning(
                "boto3 not installed — AwsSecretsManagerBackend disabled. "
                "Install boto3 in the deployment environment to enable "
                "AWS Secrets Manager-backed key resolution."
            )
            return

        # Cache the exception classes so get_secret() can `except` them
        # without re-importing on every call.
        self._client_errors = (ClientError, BotoCoreError)

        # Build kwargs only for fields the operator actually set, so boto3's
        # natural credential/region chain stays intact when the caller is
        # silent. Passing region_name=None to boto3 is fine in some versions
        # but explicit is safer.
        client_kwargs: dict[str, Any] = {}
        if self._region_name is not None:
            client_kwargs["region_name"] = self._region_name
        try:
            if self._profile_name is not None:
                session = boto3.Session(profile_name=self._profile_name)
                self._client = session.client("secretsmanager", **client_kwargs)
            else:
                self._client = boto3.client("secretsmanager", **client_kwargs)
        except self._client_errors:
            logger.warning(
                "Failed to construct AWS Secrets Manager client (region=%s)",
                self._region_name,
                exc_info=True,
            )
            return

        self._available = True

        # SPEC-025 §M-3 — log the credential source once at construction.
        # Federal auditors need to know whether prod resolved credentials
        # via IAM Instance Profile vs. env vars vs. a shared-credentials
        # file. boto3 surfaces this via the credentials object's `method`.
        self._log_credential_source()

    def _log_credential_source(self) -> None:
        """Emit a structured info log naming the credential source.

        Best-effort — botocore exposes the credential method on the
        session's `_credentials.method`. If the introspection fails,
        log a generic "credentials present" line so the auditor at
        least knows the client is configured.
        """
        if self._client is None:
            return
        method: str | None = None
        try:
            # _client is typed as Any so these private-attr lookups need no
            # ``type: ignore``; if boto3 ever changes the internals, the
            # except below catches it and we log "unknown".
            session = self._client.meta.events._unique_id_handlers
            del session  # only here to confirm the meta attr exists
            creds = self._client._request_signer._credentials
            method = getattr(creds, "method", None)
        except Exception:
            method = None
        logger.info(
            "arcllm.vault.backend_ready",
            extra={
                "backend": "aws_secrets",
                "credential_method": method or "unknown",
                "region_name": self._region_name,
                "profile_name": self._profile_name,
            },
        )

    # ------------------------------------------------------------------
    # VaultBackend protocol
    # ------------------------------------------------------------------

    def get_secret(self, path: str) -> str | None:
        """Fetch a secret by name (boto3's SecretId).

        Returns None on miss or on transient AWS unavailability. Raises
        ArcLLMConfigError on AccessDenied — that's a misconfigured IAM
        policy, not something env-var fallback should paper over.
        """
        if self._client is None:
            return None

        try:
            response = self._client.get_secret_value(SecretId=path)
        except self._client_errors as exc:
            self._handle_aws_exception(exc, path)
            return None

        if isinstance(response, dict):
            return self._extract_secret_value(response)
        return None

    def is_available(self) -> bool:
        """True if boto3 imported, the client built, and no fatal error
        has been recorded since."""
        return self._available

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _handle_aws_exception(self, exc: Exception, path: str) -> None:
        """Map AWS exceptions to the contract documented at the top.

        AccessDenied is the only case that raises — everything else logs
        and (for non-NotFound) marks the backend unavailable. Caller treats
        a non-raising return as 'no secret, possibly degraded'.
        """
        code = self._error_code(exc)
        if code in _NOT_FOUND_CODES:
            logger.debug("Secret not found in AWS Secrets Manager: %s", path)
            return
        if code in _ACCESS_DENIED_CODES:
            # Fail loud: the caller's IAM principal is wrong. Don't leak
            # the secret path into the public message — operators looking
            # at logs will already know which path they were resolving.
            raise ArcLLMConfigError(
                f"AccessDenied fetching AWS secret (code={code}). "
                f"Attach secretsmanager:GetSecretValue to the instance role."
            )
        logger.warning(
            "AWS Secrets Manager call failed (path=%s): %s", path, exc, exc_info=True
        )
        self._available = False

    @staticmethod
    def _error_code(exc: Exception) -> str:
        """Pull the ErrorCode out of a botocore ClientError; '' otherwise."""
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            error = response.get("Error")
            if isinstance(error, dict):
                code = error.get("Code")
                if isinstance(code, str):
                    return code
        return ""

    @staticmethod
    def _extract_secret_value(response: dict[str, Any]) -> str | None:
        """SecretString takes precedence; SecretBinary is decoded UTF-8."""
        if "SecretString" in response:
            value = response["SecretString"]
            return value if isinstance(value, str) else None
        if "SecretBinary" in response:
            binary = response["SecretBinary"]
            if isinstance(binary, bytes | bytearray):
                return bytes(binary).decode("utf-8")
        return None

    def __repr__(self) -> str:
        # Deliberately omits any secret material — only metadata. This
        # method may end up in tracebacks; secrets must never leak there.
        return (
            f"AwsSecretsManagerBackend(region_name={self._region_name!r}, "
            f"profile_name={self._profile_name!r}, available={self._available})"
        )
