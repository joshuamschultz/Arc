"""Tests for AwsSecretsManagerBackend — boto3-stubbed AWS Secrets Manager backend.

The tests never hit AWS. boto3.client is patched to return a MagicMock and
botocore exception classes are constructed by hand so the backend's exception
handling can be exercised deterministically.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from arcllm.exceptions import ArcLLMConfigError
from arcllm.vault import VaultBackend

# ---------------------------------------------------------------------------
# Helpers — fabricate botocore ClientError instances without importing botocore
# at the top of the module (the backend lazy-imports it; tests should mirror).
# ---------------------------------------------------------------------------


def _client_error(code: str, message: str = "boom") -> Exception:
    """Build a real botocore ClientError so the backend's `except` matches."""
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "GetSecretValue",
    )


def _no_credentials_error() -> Exception:
    from botocore.exceptions import NoCredentialsError

    return NoCredentialsError()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_backend_satisfies_vault_protocol(self) -> None:
        from arcllm.backends.aws_secrets import AwsSecretsManagerBackend

        with patch("boto3.client") as mock_client:
            mock_client.return_value = MagicMock()
            backend = AwsSecretsManagerBackend()
        assert isinstance(backend, VaultBackend)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_get_secret_returns_string_value(self) -> None:
        from arcllm.backends.aws_secrets import AwsSecretsManagerBackend

        fake_client = MagicMock()
        fake_client.get_secret_value.return_value = {"SecretString": "sk-abc-123"}

        with patch("boto3.client", return_value=fake_client):
            backend = AwsSecretsManagerBackend()
            assert backend.get_secret("arc/prod/anthropic/api_key") == "sk-abc-123"

        fake_client.get_secret_value.assert_called_once_with(
            SecretId="arc/prod/anthropic/api_key"
        )

    def test_get_secret_decodes_secret_binary(self) -> None:
        """If the secret was stored as binary, the backend decodes UTF-8."""
        from arcllm.backends.aws_secrets import AwsSecretsManagerBackend

        fake_client = MagicMock()
        fake_client.get_secret_value.return_value = {"SecretBinary": b"sk-binary-value"}

        with patch("boto3.client", return_value=fake_client):
            backend = AwsSecretsManagerBackend()
            assert backend.get_secret("arc/prod/openai/api_key") == "sk-binary-value"

    def test_is_available_true_when_client_constructed(self) -> None:
        from arcllm.backends.aws_secrets import AwsSecretsManagerBackend

        with patch("boto3.client", return_value=MagicMock()):
            backend = AwsSecretsManagerBackend()
            assert backend.is_available() is True


# ---------------------------------------------------------------------------
# Error semantics
# ---------------------------------------------------------------------------


class TestErrorSemantics:
    def test_resource_not_found_returns_none(self) -> None:
        from arcllm.backends.aws_secrets import AwsSecretsManagerBackend

        fake_client = MagicMock()
        fake_client.get_secret_value.side_effect = _client_error(
            "ResourceNotFoundException"
        )

        with patch("boto3.client", return_value=fake_client):
            backend = AwsSecretsManagerBackend()
            assert backend.get_secret("arc/prod/missing") is None

    def test_access_denied_raises_config_error(self) -> None:
        from arcllm.backends.aws_secrets import AwsSecretsManagerBackend

        fake_client = MagicMock()
        fake_client.get_secret_value.side_effect = _client_error("AccessDeniedException")

        with patch("boto3.client", return_value=fake_client):
            backend = AwsSecretsManagerBackend()
            with pytest.raises(ArcLLMConfigError, match="AccessDenied"):
                backend.get_secret("arc/prod/anthropic/api_key")

    def test_no_credentials_makes_backend_unavailable(self) -> None:
        from arcllm.backends.aws_secrets import AwsSecretsManagerBackend

        fake_client = MagicMock()
        fake_client.get_secret_value.side_effect = _no_credentials_error()

        with patch("boto3.client", return_value=fake_client):
            backend = AwsSecretsManagerBackend()
            assert backend.get_secret("arc/prod/anthropic/api_key") is None
            assert backend.is_available() is False

    def test_generic_client_error_returns_none_marks_unavailable(self) -> None:
        from arcllm.backends.aws_secrets import AwsSecretsManagerBackend

        fake_client = MagicMock()
        fake_client.get_secret_value.side_effect = _client_error("InternalServiceError")

        with patch("boto3.client", return_value=fake_client):
            backend = AwsSecretsManagerBackend()
            assert backend.get_secret("arc/prod/openai/api_key") is None
            assert backend.is_available() is False


# ---------------------------------------------------------------------------
# boto3 import failure path
# ---------------------------------------------------------------------------


class TestBoto3ImportFailure:
    def test_missing_boto3_marks_unavailable_without_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate a deployment where boto3 isn't installed.

        The backend must construct cleanly and report is_available()=False.
        get_secret() returns None. No ImportError reaches the caller — the
        VaultResolver should fall back to env vars, not crash.
        """
        # Force a fresh import of the module with boto3 hidden.
        sys.modules.pop("arcllm.backends.aws_secrets", None)

        original_import = __builtins__["__import__"] if isinstance(
            __builtins__, dict
        ) else __builtins__.__import__

        def _fake_import(
            name: str,
            globals_: Any = None,
            locals_: Any = None,
            fromlist: Any = (),
            level: int = 0,
        ) -> ModuleType:
            if name == "boto3" or name.startswith("boto3."):
                raise ImportError("No module named 'boto3'")
            return original_import(name, globals_, locals_, fromlist, level)

        monkeypatch.setattr("builtins.__import__", _fake_import)

        from arcllm.backends.aws_secrets import AwsSecretsManagerBackend

        backend = AwsSecretsManagerBackend()
        assert backend.is_available() is False
        assert backend.get_secret("arc/prod/anthropic/api_key") is None


# ---------------------------------------------------------------------------
# Region defaulting + profile passthrough
# ---------------------------------------------------------------------------


class TestRegionAndProfile:
    def test_region_defaults_to_boto3_chain(self) -> None:
        """Default region is None so boto3's natural chain (AWS_REGION env,
        ~/.aws/config, instance metadata) takes effect. Hardcoding a default
        would block per-deployment override via env var. SPEC-025 review fix.
        """
        from arcllm.backends.aws_secrets import AwsSecretsManagerBackend

        with patch("boto3.client") as mock_client:
            mock_client.return_value = MagicMock()
            AwsSecretsManagerBackend()
        # boto3.client must NOT receive a region_name kwarg — let boto3 decide.
        _, kwargs = mock_client.call_args
        assert "region_name" not in kwargs

    def test_explicit_region_passed_through(self) -> None:
        from arcllm.backends.aws_secrets import AwsSecretsManagerBackend

        with patch("boto3.client") as mock_client:
            mock_client.return_value = MagicMock()
            AwsSecretsManagerBackend(region_name="eu-west-1")
        _, kwargs = mock_client.call_args
        assert kwargs.get("region_name") == "eu-west-1"

    def test_profile_uses_session(self) -> None:
        """When profile_name is set, a boto3.Session is used instead."""
        from arcllm.backends.aws_secrets import AwsSecretsManagerBackend

        fake_session = MagicMock()
        fake_session.client.return_value = MagicMock()

        with patch("boto3.Session", return_value=fake_session) as mock_session_ctor:
            AwsSecretsManagerBackend(profile_name="dev", region_name="us-west-2")

        mock_session_ctor.assert_called_once_with(profile_name="dev")
        fake_session.client.assert_called_once_with(
            "secretsmanager", region_name="us-west-2"
        )


# ---------------------------------------------------------------------------
# Defense against secret leakage in repr/str
# ---------------------------------------------------------------------------


class TestNoSecretsInRepr:
    def test_repr_never_includes_fetched_secret(self) -> None:
        from arcllm.backends.aws_secrets import AwsSecretsManagerBackend

        fake_client = MagicMock()
        fake_client.get_secret_value.return_value = {"SecretString": "sk-super-secret"}

        with patch("boto3.client", return_value=fake_client):
            backend = AwsSecretsManagerBackend()
            backend.get_secret("arc/prod/anthropic/api_key")

        # repr/str must not contain the secret value or the secret path —
        # both could end up in tracebacks / logs.
        assert "sk-super-secret" not in repr(backend)
        assert "sk-super-secret" not in str(backend)
        # Repr exposes only safe metadata: class name + non-secret config.
        rendered = repr(backend)
        assert "AwsSecretsManagerBackend" in rendered
        assert "available=" in rendered


# ---------------------------------------------------------------------------
# Constructor signature stability — VaultResolver.from_config builds with
# keyword args from TOML's [llm.vault] section (excluding `backend`).
# ---------------------------------------------------------------------------


class TestConstructorAcceptsKwargs:
    def test_kwargs_only_init_does_not_raise(self) -> None:
        from arcllm.backends.aws_secrets import AwsSecretsManagerBackend

        with patch("boto3.client", return_value=MagicMock()):
            # All optional, all kw-only by convention.
            backend = AwsSecretsManagerBackend(region_name="us-east-1")
            assert backend.is_available() is True
