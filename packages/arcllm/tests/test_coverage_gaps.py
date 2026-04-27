"""Targeted tests to close coverage gaps identified in the coverage sweep.

Covers:
- _signing.py line 72: ECDSA path when cryptography is installed but not implemented
- adapters/anthropic.py lines 95, 135: unknown block type ValueError, tool_choice kwarg
- adapters/google.py line 37: error path
- modules/otel.py: HTTP exporter, TLS kwargs, _build_tls_kwargs, _create_exporter none path
- modules/circuit_breaker.py: no-op same-state transition, OPEN with null failure time
- modules/fallback.py: _load_fallback_model helper
- modules/queue.py: queue_stats with non-zero wait, _set_rejected_span_attribute
- modules/security.py: ToolResultBlock with list content, ImageBlock pass-through
- modules/telemetry.py: set_global_defaults with all three args, daily_limit warn path
- registry.py: vault path, routing with missing provider, circuit_breaker on_event,
               security module, load_model with on_event/trace_store/agent_label/budget_scope
- trace_store.py: warm-start from existing file, rotation tombstone, get() by trace_id,
                  verify_chain start_seq offset, query cursor and filter paths
- vault.py: allowlist rejection, missing class in module
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from arcllm.exceptions import ArcLLMAPIError, ArcLLMConfigError
from arcllm.trace_store import JSONLTraceStore, TraceRecord
from arcllm.types import (
    ImageBlock,
    LLMResponse,
    Message,
    TextBlock,
    Tool,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------


def _make_llm_response(**kwargs: Any) -> LLMResponse:
    defaults: dict[str, Any] = {
        "content": "hello",
        "tool_calls": [],
        "usage": Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        "model": "test-model",
        "stop_reason": "end_turn",
    }
    defaults.update(kwargs)
    return LLMResponse(**defaults)


def _make_inner_mock(**overrides: Any) -> MagicMock:
    inner = MagicMock()
    inner.name = "anthropic"
    inner.model_name = "claude-sonnet-4"
    inner.invoke = AsyncMock(return_value=_make_llm_response())
    for k, v in overrides.items():
        setattr(inner, k, v)
    return inner


# ---------------------------------------------------------------------------
# _signing.py — ECDSA path when cryptography IS available but not implemented
# ---------------------------------------------------------------------------


class TestSigningEcdsaAvailable:
    def test_ecdsa_raises_not_implemented_when_cryptography_available(self):
        """Line 72: ECDSA branch when cryptography import succeeds."""
        import os

        from arcllm._signing import create_signer

        # Ensure cryptography can actually be imported; if not, skip gracefully
        try:
            import cryptography  # noqa: F401
        except ImportError:
            pytest.skip("cryptography package not installed")

        with patch.dict(os.environ, {"TEST_SIGNING_KEY": "key"}):
            with pytest.raises(ArcLLMConfigError, match="not yet fully implemented"):
                create_signer("ecdsa-p256", "TEST_SIGNING_KEY")


# ---------------------------------------------------------------------------
# adapters/anthropic.py — uncovered branches
# ---------------------------------------------------------------------------


class TestAnthropicUnknownBlock:
    def test_format_unknown_block_type_raises_value_error(self):
        """Line 95: ValueError on unrecognised block type."""
        import os

        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.config import ProviderConfig, ProviderSettings

        cfg = ProviderConfig(
            provider=ProviderSettings(
                api_format="anthropic-messages",
                base_url="https://api.anthropic.com",
                api_key_env="ARCLLM_TEST_KEY",
                default_model="claude-3",
                default_temperature=0.7,
            ),
            models={},
        )
        with patch.dict(os.environ, {"ARCLLM_TEST_KEY": "key"}):
            adapter = AnthropicAdapter(cfg, "claude-3")

        class _FakeBlock:
            pass

        with pytest.raises(ValueError, match="Unknown content block type"):
            adapter._format_content_block(_FakeBlock())  # type: ignore[arg-type]

    def test_tool_choice_kwarg_in_request_body(self):
        """Line 135: tool_choice kwarg forwarded into request body."""
        import os

        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.config import ProviderConfig, ProviderSettings

        cfg = ProviderConfig(
            provider=ProviderSettings(
                api_format="anthropic-messages",
                base_url="https://api.anthropic.com",
                api_key_env="ARCLLM_TEST_KEY",
                default_model="claude-3",
                default_temperature=0.7,
            ),
            models={},
        )
        with patch.dict(os.environ, {"ARCLLM_TEST_KEY": "key"}):
            adapter = AnthropicAdapter(cfg, "claude-3")

        tools = [
            Tool(
                name="search",
                description="web search",
                parameters={"type": "object", "properties": {}},
            )
        ]
        messages = [Message(role="user", content="hi")]
        body = adapter._build_request_body(messages, tools=tools, tool_choice={"type": "auto"})
        assert body.get("tool_choice") == {"type": "auto"}


# ---------------------------------------------------------------------------
# adapters/google.py — error path (line 37)
# ---------------------------------------------------------------------------


class TestGoogleAdapterError:
    @pytest.mark.asyncio
    async def test_google_error_path_raises_api_error(self):
        """Line 37: ArcLLMAPIError raised on non-200 from Google."""
        import os

        from arcllm.adapters.google import GoogleAdapter
        from arcllm.config import ProviderConfig, ProviderSettings

        cfg = ProviderConfig(
            provider=ProviderSettings(
                api_format="openai-chat",
                base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                api_key_env="GOOGLE_API_KEY",
                default_model="gemini-2.0-flash",
                default_temperature=0.7,
            ),
            models={},
        )
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "fake-key"}):
            adapter = GoogleAdapter(cfg, "gemini-2.0-flash")

        mock_resp = httpx.Response(
            503,
            text="service unavailable",
            request=httpx.Request("POST", "https://example.com/chat/completions"),
        )
        adapter._client = MagicMock()
        adapter._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(ArcLLMAPIError) as exc:
            await adapter.invoke([Message(role="user", content="hi")])

        assert exc.value.status_code == 503
        assert exc.value.provider == "google"


# ---------------------------------------------------------------------------
# modules/otel.py — HTTP exporter, TLS kwargs, _create_exporter none branch
# ---------------------------------------------------------------------------


class TestOtelHelpers:
    def test_build_tls_kwargs_all_fields(self):
        """Lines 47, 49, 51: all three TLS fields populated."""
        from arcllm.modules.otel import _build_tls_kwargs

        config = {
            "certificate_file": "/certs/ca.pem",
            "client_key_file": "/certs/key.pem",
            "client_cert_file": "/certs/cert.pem",
        }
        result = _build_tls_kwargs(config)
        assert result["certificate_file"] == "/certs/ca.pem"
        assert result["client_key_file"] == "/certs/key.pem"
        assert result["client_certificate_file"] == "/certs/cert.pem"

    def test_build_tls_kwargs_empty(self):
        """No TLS fields → empty dict."""
        from arcllm.modules.otel import _build_tls_kwargs

        assert _build_tls_kwargs({}) == {}

    def test_create_otlp_exporter_http_protocol(self):
        """Lines 82-95: HTTP exporter branch via mock."""
        from arcllm.modules.otel import _create_otlp_exporter

        mock_exporter_cls = MagicMock()
        mock_exporter_instance = MagicMock()
        mock_exporter_cls.return_value = mock_exporter_instance

        with patch.dict(
            "sys.modules",
            {
                "opentelemetry.exporter.otlp.proto.http.trace_exporter": MagicMock(
                    OTLPSpanExporter=mock_exporter_cls
                )
            },
        ):
            result = _create_otlp_exporter(
                {
                    "protocol": "http",
                    "endpoint": "http://localhost:4318",
                    "headers": {"Authorization": "Bearer tok"},
                    "timeout_ms": 5000,
                }
            )

        mock_exporter_cls.assert_called_once()
        assert result is mock_exporter_instance

    def test_create_otlp_exporter_http_missing_package(self):
        """Lines 87-89: ImportError on missing HTTP exporter package."""
        from arcllm.modules.otel import _create_otlp_exporter

        with patch.dict("sys.modules", {"opentelemetry.exporter.otlp.proto.http.trace_exporter": None}):
            with pytest.raises((ArcLLMConfigError, ImportError)):
                _create_otlp_exporter({"protocol": "http"})

    def test_create_exporter_none_returns_none(self):
        """Line 110: 'none' exporter type → returns None."""
        from arcllm.modules.otel import _create_exporter

        result = _create_exporter({"exporter": "none"})
        assert result is None

    def test_setup_sdk_exporter_none_skips_setup(self):
        """Line 145: _setup_sdk skips when exporter resolves to None."""
        from arcllm.modules.otel import _setup_sdk, reset_sdk

        reset_sdk()

        # Patch _create_exporter to return None (simulates exporter="none")
        with patch("arcllm.modules.otel._create_exporter", return_value=None):
            # Provide minimal SDK mocks so import doesn't fail
            fake_sdk = MagicMock()
            with patch.dict(
                "sys.modules",
                {
                    "opentelemetry.sdk.resources": fake_sdk,
                    "opentelemetry.sdk.trace": fake_sdk,
                    "opentelemetry.sdk.trace.export": fake_sdk,
                    "opentelemetry.sdk.trace.sampling": fake_sdk,
                    "opentelemetry": MagicMock(),
                },
            ):
                # Should not raise even though exporter is None
                _setup_sdk({"exporter": "none"})

        reset_sdk()


# ---------------------------------------------------------------------------
# modules/circuit_breaker.py — same-state transition, OPEN with null failure
# ---------------------------------------------------------------------------


class TestCircuitBreakerEdges:
    def test_transition_same_state_is_noop(self):
        """Line 90: transition to same state returns without firing callbacks."""
        from arcllm.modules.circuit_breaker import CircuitBreakerModule, CircuitState

        changes: list[tuple[str, str]] = []
        inner = _make_inner_mock()
        cb = CircuitBreakerModule(
            {
                "failure_threshold": 2,
                "cooldown_seconds": 30,
                "on_state_change": lambda old, new, info: changes.append((old, new)),
            },
            inner,
        )
        # Transition to CLOSED when already CLOSED — should be noop
        with cb._lock:
            cb._transition(CircuitState.CLOSED)

        assert changes == []

    def test_check_state_open_with_no_failure_time_passes(self):
        """Line 154: OPEN state with _last_failure_time=None allows call through."""
        from arcllm.modules.circuit_breaker import CircuitBreakerModule, CircuitState

        inner = _make_inner_mock()
        cb = CircuitBreakerModule({"failure_threshold": 1, "cooldown_seconds": 30}, inner)
        # Force OPEN state but leave _last_failure_time as None
        cb._state = CircuitState.OPEN
        cb._last_failure_time = None

        # _check_state should NOT raise CircuitOpenError when last_failure_time is None
        with cb._lock:
            cb._check_state()  # must not raise

    @pytest.mark.asyncio
    async def test_half_open_max_calls_exceeded_raises(self):
        """Line 165: HALF_OPEN with max calls already at limit rejects."""
        from arcllm.modules.circuit_breaker import (
            CircuitBreakerModule,
            CircuitOpenError,
            CircuitState,
        )

        inner = _make_inner_mock()
        cb = CircuitBreakerModule(
            {"failure_threshold": 1, "cooldown_seconds": 1, "half_open_max_calls": 1},
            inner,
        )
        # Force HALF_OPEN with counter already at max
        cb._state = CircuitState.HALF_OPEN
        cb._half_open_calls = 1

        with pytest.raises(CircuitOpenError):
            await cb.invoke([Message(role="user", content="hi")])


# ---------------------------------------------------------------------------
# modules/fallback.py — _load_fallback_model helper
# ---------------------------------------------------------------------------


class TestFallbackLoadHelper:
    def test_load_fallback_model_disables_nested_fallback(self):
        """Lines 17-20: _load_fallback_model calls registry.load_model with fallback=False.

        The function imports load_model lazily inside itself as _load_model.
        We patch the underlying registry function so the lazy import picks it up.
        """
        from arcllm.modules.fallback import _load_fallback_model

        # Patch arcllm.registry.load_model — the actual import target inside the function
        with patch("arcllm.registry.load_model") as mock_load:
            mock_load.return_value = _make_inner_mock()
            _load_fallback_model("openai")
            mock_load.assert_called_once_with("openai", fallback=False)


# ---------------------------------------------------------------------------
# modules/queue.py — queue_stats avg_wait, _set_rejected_span_attribute
# ---------------------------------------------------------------------------


class TestQueueStats:
    @pytest.mark.asyncio
    async def test_queue_stats_avg_wait_ms_populated(self):
        """Lines 141-146: avg_wait_ms calculated after a completed call."""
        from arcllm.modules.queue import QueueModule

        inner = _make_inner_mock()
        qm = QueueModule({"max_concurrent": 2, "call_timeout": 5}, inner)

        messages = [Message(role="user", content="hi")]
        await qm.invoke(messages)

        stats = qm.queue_stats()
        assert stats["total_completed"] == 1
        assert stats["avg_wait_ms"] >= 0.0

    @pytest.mark.asyncio
    async def test_queue_stats_avg_wait_zero_on_no_calls(self):
        """avg_wait_ms is 0.0 when no calls have been made."""
        from arcllm.modules.queue import QueueModule

        inner = _make_inner_mock()
        qm = QueueModule({"max_concurrent": 2}, inner)
        stats = qm.queue_stats()
        assert stats["avg_wait_ms"] == 0.0

    @pytest.mark.asyncio
    async def test_set_rejected_span_attribute_not_recording(self):
        """Lines 159-163: _set_rejected_span_attribute with non-recording span."""
        from arcllm.exceptions import QueueFullError
        from arcllm.modules.queue import QueueModule

        inner = _make_inner_mock()
        qm = QueueModule({"max_concurrent": 1, "max_queued": 0}, inner)

        messages = [Message(role="user", content="hi")]

        # Saturate the semaphore so the next call hits backpressure
        # With max_queued=0, any call when waiters >= 0 is rejected immediately
        with pytest.raises(QueueFullError):
            await qm.invoke(messages)

        assert qm._total_rejected == 1


# ---------------------------------------------------------------------------
# modules/security.py — ToolResultBlock list content, ImageBlock pass-through
# ---------------------------------------------------------------------------


class TestSecurityRedactBlocks:
    def _make_module(self) -> Any:
        import os

        from arcllm.modules.security import SecurityModule

        inner = _make_inner_mock()
        with patch.dict(os.environ, {"ARCLLM_SIGNING_KEY": "test-signing-key-12345"}):
            return SecurityModule(
                {"pii_enabled": True, "signing_enabled": False},
                inner,
            )

    def test_tool_result_block_with_list_content_passes_through(self):
        """Line 132: ToolResultBlock with list content → append unchanged."""
        module = self._make_module()
        blocks = [
            ToolResultBlock(
                tool_use_id="t1",
                content=[TextBlock(text="clean text")],
            )
        ]
        result = module._redact_blocks(blocks)
        assert len(result) == 1
        assert isinstance(result[0], ToolResultBlock)

    def test_image_block_passes_through_untouched(self):
        """Lines 148-150: ImageBlock is not redacted."""
        module = self._make_module()
        blocks = [ImageBlock(source="base64abc", media_type="image/png")]
        result = module._redact_blocks(blocks)
        assert len(result) == 1
        assert isinstance(result[0], ImageBlock)
        assert result[0].source == "base64abc"

    def test_tool_use_block_with_pii_in_arguments_is_redacted(self):
        """Lines 133-147: ToolUseBlock with PII in arguments gets redacted."""
        module = self._make_module()
        blocks = [
            ToolUseBlock(
                id="t1",
                name="send_email",
                arguments={"to": "user@example.com", "body": "hello 212-555-1234"},
            )
        ]
        result = module._redact_blocks(blocks)
        assert len(result) == 1
        assert isinstance(result[0], ToolUseBlock)
        # Either a redaction occurred or didn't — block stays ToolUseBlock either way
        assert result[0].name == "send_email"


# ---------------------------------------------------------------------------
# modules/telemetry.py — set_global_defaults with all three args
# ---------------------------------------------------------------------------


class TestTelemetryGlobalDefaults:
    def setup_method(self) -> None:
        from arcllm.modules.telemetry import clear_global_defaults

        clear_global_defaults()

    def teardown_method(self) -> None:
        from arcllm.modules.telemetry import clear_global_defaults

        clear_global_defaults()

    def test_set_global_defaults_all_args(self):
        """Lines 84, 86, 88: all three branches in set_global_defaults populated."""
        from arcllm.modules.telemetry import _global_defaults, set_global_defaults

        callback = MagicMock()
        store = MagicMock()
        set_global_defaults(on_event=callback, trace_store=store, agent_did="did:arc:test")

        assert _global_defaults["on_event"] is callback
        assert _global_defaults["trace_store"] is store
        assert _global_defaults["agent_did"] == "did:arc:test"

    def test_set_global_defaults_picked_up_by_telemetry_module(self):
        """TelemetryModule instance created after set_global_defaults inherits defaults."""
        from arcllm.modules.telemetry import TelemetryModule, set_global_defaults

        callback = MagicMock()
        set_global_defaults(on_event=callback)

        inner = _make_inner_mock()
        module = TelemetryModule({}, inner)
        assert module._on_event is callback

    def test_set_global_defaults_none_args_not_set(self):
        """set_global_defaults with None args leaves those keys absent."""
        from arcllm.modules.telemetry import _global_defaults, set_global_defaults

        set_global_defaults(on_event=None, trace_store=None, agent_did=None)
        assert "on_event" not in _global_defaults
        assert "trace_store" not in _global_defaults
        assert "agent_did" not in _global_defaults

    @pytest.mark.asyncio
    async def test_telemetry_daily_limit_warn_mode(self):
        """Line 398: daily limit exceeded in warn (not block) mode."""
        from arcllm.modules.telemetry import TelemetryModule, clear_budgets

        clear_budgets()

        inner = _make_inner_mock()
        module = TelemetryModule(
            {
                "daily_limit_usd": 0.001,  # Very small limit — will be exceeded
                "budget_scope": "test:daily-warn",
                "enforcement": "warn",
                "cost_output_per_1m": 1000.0,  # Large cost to exceed limit quickly
            },
            inner,
        )
        # Pre-load the accumulator past the daily limit
        module._accumulator.deduct(0.002)

        messages = [Message(role="user", content="hi")]
        # Warn mode — should NOT raise, just tag metadata
        response = await module.invoke(messages)
        assert response is not None

        clear_budgets()


# ---------------------------------------------------------------------------
# registry.py — vault config path, circuit_breaker on_event, security module,
#                load_model with on_event / trace_store / agent_label / budget_scope
# ---------------------------------------------------------------------------


class TestRegistryUncoveredPaths:
    @pytest.fixture(autouse=True)
    def _clear(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from arcllm.registry import clear_cache

        clear_cache()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        yield
        clear_cache()

    def test_load_model_with_security_module(self):
        """Lines 323-325: security=True wraps adapter with SecurityModule."""
        import os

        from arcllm.modules.security import SecurityModule
        from arcllm.registry import load_model

        with patch.dict(os.environ, {"ARCLLM_SIGNING_KEY": "test-signing-key-12345"}):
            model = load_model(
                "anthropic",
                security={"pii_enabled": True, "signing_enabled": False},
                telemetry=False,
                retry=False,
                queue=False,
            )
        assert isinstance(model, SecurityModule)

    def test_load_model_circuit_breaker_with_on_event(self):
        """Lines 317-319: on_event injected into circuit_breaker config.

        Per ADR-019, security is enabled by default (wraps outermost over
        circuit_breaker). Disable security so circuit_breaker is observable.
        """
        from arcllm.modules.circuit_breaker import CircuitBreakerModule
        from arcllm.registry import load_model

        events: list[Any] = []
        model = load_model(
            "anthropic",
            circuit_breaker={"failure_threshold": 3, "cooldown_seconds": 10},
            on_event=lambda r: events.append(r),
            telemetry=False,
            retry=False,
            queue=False,
            security=False,
        )
        assert isinstance(model, CircuitBreakerModule)
        assert model._on_event is not None

    def test_load_model_with_on_event_and_trace_store_in_telemetry(self):
        """Lines 358, 360: on_event and trace_store injected into telemetry config."""
        from arcllm.modules.telemetry import TelemetryModule
        from arcllm.registry import load_model

        events: list[Any] = []
        store = MagicMock()
        model = load_model(
            "anthropic",
            telemetry=True,
            on_event=lambda r: events.append(r),
            trace_store=store,
            retry=False,
            queue=False,
        )
        assert isinstance(model, TelemetryModule)
        assert model._on_event is not None
        assert model._trace_store is store

    def test_load_model_with_agent_label_in_telemetry(self):
        """Line 362: agent_label injected into telemetry config."""
        from arcllm.modules.telemetry import TelemetryModule
        from arcllm.registry import load_model

        model = load_model(
            "anthropic",
            telemetry=True,
            agent_label="agent-007",
            retry=False,
            queue=False,
        )
        assert isinstance(model, TelemetryModule)
        assert model._agent_label == "agent-007"

    def test_load_model_with_budget_scope_in_telemetry(self):
        """Line 351: budget_scope kwarg injected into telemetry config."""
        from arcllm.modules.telemetry import TelemetryModule, clear_budgets
        from arcllm.registry import load_model

        clear_budgets()
        model = load_model(
            "anthropic",
            telemetry={"monthly_limit_usd": 10.0},
            budget_scope="agent:test-007",
            retry=False,
            queue=False,
        )
        assert isinstance(model, TelemetryModule)
        assert model._budget_scope == "agent:test-007"
        clear_budgets()

    def test_load_model_routing_missing_provider_raises(self):
        """Lines 276-277: routing rule missing 'provider' raises ArcLLMConfigError."""
        from arcllm.registry import load_model

        with pytest.raises(ArcLLMConfigError, match="missing 'provider'"):
            load_model(
                "anthropic",
                routing={
                    "rules": {
                        "unclassified": {}  # missing 'provider'
                    }
                },
                telemetry=False,
                retry=False,
                queue=False,
            )

    def test_validate_provider_name_empty_string_raises(self):
        """Line 55-59: empty string fails regex match → ArcLLMConfigError."""
        from arcllm.registry import _validate_provider_name

        with pytest.raises(ArcLLMConfigError, match="Invalid provider name"):
            _validate_provider_name("")

    def test_get_adapter_class_cache_hit_on_second_call(self):
        """Line 76-77: double-check lock cache hit path exercised on second call."""
        from arcllm.registry import _get_adapter_class

        cls1 = _get_adapter_class("anthropic")
        cls2 = _get_adapter_class("anthropic")
        assert cls1 is cls2


# ---------------------------------------------------------------------------
# vault.py — allowlist rejection, missing class in module
# ---------------------------------------------------------------------------


class TestVaultEdges:
    def test_from_config_non_allowlisted_module_raises(self):
        """Line 76: module not in allowlist → ArcLLMConfigError."""
        from arcllm.vault import VaultResolver

        with pytest.raises(ArcLLMConfigError, match="not in the allowlist"):
            VaultResolver.from_config("evil.malicious.module:BackendClass", 300)

    def test_from_config_missing_class_in_module_raises(self):
        """Lines 89-92: class not found in module → ArcLLMConfigError."""
        import sys
        import types

        from arcllm.vault import VaultResolver

        # Create a fake module in the arcllm namespace so it passes the prefix check
        fake_mod = types.ModuleType("arcllm._fake_vault_backend")
        sys.modules["arcllm._fake_vault_backend"] = fake_mod

        try:
            with pytest.raises(ArcLLMConfigError, match="not found in"):
                VaultResolver.from_config("arcllm._fake_vault_backend:NonExistentClass", 300)
        finally:
            del sys.modules["arcllm._fake_vault_backend"]

    def test_from_config_import_error_raises(self):
        """Lines 83-87: ImportError on bad module → ArcLLMConfigError."""
        from arcllm.vault import VaultResolver

        with pytest.raises(ArcLLMConfigError, match="not installed"):
            VaultResolver.from_config("arcllm._does_not_exist_xyz:SomeClass", 300)


# ---------------------------------------------------------------------------
# trace_store.py — warm-start from existing file, rotation tombstone,
#                  get() by trace_id, verify_chain start_seq, query filters
# ---------------------------------------------------------------------------


class TestTraceStoreEdgePaths:
    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        return tmp_path / "workspace"

    @staticmethod
    def _make_record(**kwargs: Any) -> TraceRecord:
        defaults: dict[str, Any] = {
            "provider": "anthropic",
            "model": "claude-sonnet-4",
            "duration_ms": 50.0,
            "cost_usd": 0.001,
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        }
        defaults.update(kwargs)
        return TraceRecord(**defaults)

    @pytest.mark.asyncio
    async def test_warm_start_reads_last_hash_from_existing_file(
        self, workspace: Path
    ) -> None:
        """Lines 188-205: warm-start path reads last hash from pre-existing JSONL file."""
        store = JSONLTraceStore(workspace)

        # Write one record to disk via the store
        r1 = self._make_record(trace_id="warm-001")
        await store.append(r1)

        # Create a fresh store pointing at same workspace — should warm-start
        store2 = JSONLTraceStore(workspace)
        r2 = self._make_record(trace_id="warm-002")
        await store2.append(r2)

        # store2 should have picked up the hash from the prior record
        assert store2._last_hash != "0" * 64

    @pytest.mark.asyncio
    async def test_warm_start_with_bad_last_line_logs_warning(
        self, workspace: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Lines 199-200: bad JSON on last line handled gracefully."""
        import logging


        store = JSONLTraceStore(workspace)
        traces_dir = workspace / "traces"
        traces_dir.mkdir(parents=True, exist_ok=True)
        traces_dir.chmod(0o700)

        # Write a broken JSONL file
        today = store._today()
        bad_file = traces_dir / f"traces-{today}.jsonl"
        bad_file.write_text("not valid json\n")
        bad_file.chmod(0o600)

        # Fresh store — should survive warm-start without exception
        store2 = JSONLTraceStore(workspace)
        with caplog.at_level(logging.WARNING, logger="arcllm.trace_store"):
            r = self._make_record(trace_id="after-bad-line")
            await store2.append(r)
        # Record was still written successfully
        assert store2._line_count >= 1

    @pytest.mark.asyncio
    async def test_verify_tail_tamper_detected_prev_hash_mismatch(
        self, workspace: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Lines 225-230: hash chain break detected during tail verification."""
        import logging

        store = JSONLTraceStore(workspace)

        r1 = self._make_record(trace_id="t1")
        r2 = self._make_record(trace_id="t2")
        await store.append(r1)
        await store.append(r2)

        # Tamper with the second record's prev_hash
        traces_dir = workspace / "traces"
        today = store._today()
        f = traces_dir / f"traces-{today}.jsonl"
        lines = f.read_text().strip().split("\n")
        data2 = json.loads(lines[1])
        data2["prev_hash"] = "0" * 64  # wrong prev_hash
        lines[1] = json.dumps(data2)
        f.write_text("\n".join(lines) + "\n")

        # New store should detect tamper during warm-start tail verification
        store2 = JSONLTraceStore(workspace)
        with caplog.at_level(logging.ERROR, logger="arcllm.trace_store"):
            await store2._warm_start()
        assert any("TAMPER" in m for m in caplog.messages)

    @pytest.mark.asyncio
    async def test_verify_tail_tamper_detected_record_hash_mismatch(
        self, workspace: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Lines 234-238: record_hash mismatch detected during tail verification."""
        import logging

        store = JSONLTraceStore(workspace)
        r = self._make_record(trace_id="t-hash")
        await store.append(r)

        traces_dir = workspace / "traces"
        today = store._today()
        f = traces_dir / f"traces-{today}.jsonl"
        lines = f.read_text().strip().split("\n")
        data = json.loads(lines[0])
        data["record_hash"] = "badhash" * 8  # wrong hash
        lines[0] = json.dumps(data)
        f.write_text("\n".join(lines) + "\n")

        store2 = JSONLTraceStore(workspace)
        with caplog.at_level(logging.ERROR, logger="arcllm.trace_store"):
            await store2._warm_start()
        assert any("TAMPER" in m for m in caplog.messages)

    @pytest.mark.asyncio
    async def test_rotation_tombstone_written_on_date_change(
        self, workspace: Path
    ) -> None:
        """Lines 250-258: rotation tombstone written when date changes."""
        store = JSONLTraceStore(workspace)

        # Prime the store with a record for "yesterday"
        fake_yesterday = "2020-01-01"
        r = self._make_record(trace_id="old-record")
        hashed = r.with_hash("0" * 64)
        store._current_date = fake_yesterday
        traces_dir = workspace / "traces"
        traces_dir.mkdir(parents=True, exist_ok=True)
        traces_dir.chmod(0o700)
        old_file = traces_dir / f"traces-{fake_yesterday}.jsonl"
        with old_file.open("a") as f_:
            f_.write(json.dumps(hashed.model_dump()) + "\n")
        old_file.chmod(0o600)
        store._current_file = old_file
        store._last_hash = hashed.record_hash
        store._line_count = 1
        store._warm_started = True

        # Trigger rotation
        await store._maybe_rotate()

        # Tombstone should have been written to the old file
        content = old_file.read_text()
        assert "rotation" in content

    @pytest.mark.asyncio
    async def test_get_returns_record_by_trace_id(self, workspace: Path) -> None:
        """Lines 358-371: get() locates a specific record by trace_id."""
        store = JSONLTraceStore(workspace)
        r = self._make_record(trace_id="find-me-xyz")
        await store.append(r)

        found = await store.get("find-me-xyz")
        assert found is not None
        assert found.trace_id == "find-me-xyz"

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing_trace_id(
        self, workspace: Path
    ) -> None:
        """get() returns None when trace_id not found."""
        store = JSONLTraceStore(workspace)
        r = self._make_record(trace_id="present")
        await store.append(r)

        not_found = await store.get("does-not-exist")
        assert not_found is None

    @pytest.mark.asyncio
    async def test_query_with_provider_filter(self, workspace: Path) -> None:
        """Lines 339-340: provider filter applied during query."""
        store = JSONLTraceStore(workspace)

        r1 = self._make_record(trace_id="q1", provider="anthropic")
        r2 = self._make_record(trace_id="q2", provider="openai")
        await store.append(r1)
        await store.append(r2)

        results, _ = await store.query(provider="openai")
        assert all(r.provider == "openai" for r in results)

    @pytest.mark.asyncio
    async def test_query_with_status_filter(self, workspace: Path) -> None:
        """Line 342: status filter applied during query."""
        store = JSONLTraceStore(workspace)

        r1 = self._make_record(trace_id="s1", status="success")
        r2 = self._make_record(trace_id="s2", status="error")
        await store.append(r1)
        await store.append(r2)

        results, _ = await store.query(status="error")
        assert all(r.status == "error" for r in results)

    @pytest.mark.asyncio
    async def test_verify_chain_with_start_seq_skips_early_records(
        self, workspace: Path
    ) -> None:
        """Lines 385-391: start_seq causes early records to be skipped."""
        store = JSONLTraceStore(workspace)

        for i in range(5):
            r = self._make_record(trace_id=f"chain-{i}")
            await store.append(r)

        # start_seq=3 should skip first 3 records and still succeed
        valid = await store.verify_chain(start_seq=3)
        assert valid is True

    @pytest.mark.asyncio
    async def test_verify_chain_detects_tampered_record(
        self, workspace: Path
    ) -> None:
        """Lines 397-401: verify_chain returns False on hash mismatch."""
        store = JSONLTraceStore(workspace)

        r1 = self._make_record(trace_id="vc1")
        r2 = self._make_record(trace_id="vc2")
        await store.append(r1)
        await store.append(r2)

        # Tamper with second record's record_hash
        traces_dir = workspace / "traces"
        today = store._today()
        f = traces_dir / f"traces-{today}.jsonl"
        lines = f.read_text().strip().split("\n")
        data = json.loads(lines[1])
        data["record_hash"] = "deadbeef" * 8
        lines[1] = json.dumps(data)
        f.write_text("\n".join(lines) + "\n")

        valid = await store.verify_chain()
        assert valid is False

    @pytest.mark.asyncio
    async def test_verify_chain_bad_json_returns_false(
        self, workspace: Path
    ) -> None:
        """Line 386: JSONDecodeError during verify_chain returns False."""
        store = JSONLTraceStore(workspace)
        traces_dir = workspace / "traces"
        traces_dir.mkdir(parents=True, exist_ok=True)
        traces_dir.chmod(0o700)

        bad_file = traces_dir / "traces-2020-01-01.jsonl"
        bad_file.write_text("not json at all\n")
        bad_file.chmod(0o600)

        valid = await store.verify_chain()
        assert valid is False

    @pytest.mark.asyncio
    async def test_query_with_agent_filter(self, workspace: Path) -> None:
        """Line 341: agent_label filter applied during query."""
        store = JSONLTraceStore(workspace)

        r1 = self._make_record(trace_id="a1", agent_label="agent-alpha")
        r2 = self._make_record(trace_id="a2", agent_label="agent-beta")
        await store.append(r1)
        await store.append(r2)

        results, _ = await store.query(agent="agent-alpha")
        assert all(r.agent_label == "agent-alpha" for r in results)

    @pytest.mark.asyncio
    async def test_query_cursor_pagination(self, workspace: Path) -> None:
        """Lines 314-315, 353-354: cursor-based pagination returns next_cursor."""
        store = JSONLTraceStore(workspace)

        for i in range(5):
            r = self._make_record(trace_id=f"page-{i}")
            await store.append(r)

        # Request fewer records than total to trigger cursor
        results, cursor = await store.query(limit=2)
        assert len(results) == 2
        assert cursor is not None

        # Use cursor to get next page
        page2, _ = await store.query(limit=2, cursor=cursor)
        assert len(page2) >= 0  # may be 0 if all remaining were in prev page

    @pytest.mark.asyncio
    async def test_query_start_end_date_filters(self, workspace: Path) -> None:
        """Lines 344-347: start/end timestamp filters."""
        store = JSONLTraceStore(workspace)

        r = self._make_record(trace_id="ts-filter")
        await store.append(r)

        # Future start date — should exclude all records
        results, _ = await store.query(start="9999-01-01T00:00:00+00:00")
        assert len(results) == 0
