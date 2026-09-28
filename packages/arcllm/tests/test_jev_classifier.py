"""Jev classifier plugin — ``arcllm.classifiers.jev`` (SPEC-083 T-1199, COMP-019).

``typesafe-sdk`` is an OPTIONAL extra (``arcllm[jev]``) and is not installed in
the dev venv, so the plugin is exercised two ways:

1. **Fake SDK (always runs).** A stand-in ``typesafe_sdk`` module is placed in
   ``sys.modules`` with the SDK's verified surface (0.7.2):
   ``AsyncTypeSafeClient(*, api_key=None, model=None, retry=None, timeout=None,
   headers=None, transport=None, http_client=None, base_url=None)`` — including
   its real fallback to ``TYPESAFE_API_KEY`` / ``TYPESAFE_BASE_URL`` when an
   argument is ``None`` and its debug logging of request/response BODIES on the
   ``typesafe_sdk`` logger. The fake normalizes the questions it is handed into
   the documented wire dict so the body shape can be asserted without the SDK.
2. **Real wire (skips when the SDK is absent).** ``TestRealSdkWire`` injects an
   ``httpx2.MockTransport`` through the plugin's ``transport=`` seam and asserts
   the actual HTTP request. These need ``typesafe_sdk`` + ``httpx2`` and are the
   only ``importorskip`` tests — the request bytes are produced by the SDK, so
   they cannot be checked without it.

Assumed plugin constructor (keyword-only in these tests)::

    JevClassifier(
        model: str,                               # pinned, e.g. "jev-1.13"
        *,
        vault_resolver: VaultResolver | None = None,  # None -> env-only VaultResolver
        vault_path: str | None = None,
        api_key_env: str = "TYPESAFE_API_KEY",
        base_url: str = "https://api.typesafe.ai",   # ALWAYS passed explicitly
        timeout: float = 10.0,
        transport: object | None = None,             # forwarded to AsyncTypeSafeClient
    )

``typesafe_sdk`` is imported lazily per call (never at module import), the key
is resolved per call and passed explicitly as ``api_key=``, and the plugin
silences the SDK's body logging.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import math
import os
import sys
import time
import types
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from arcllm.classifiers.jev import JevClassifier
from arcllm.classify import (
    ChoiceResult,
    ChoiceSpec,
    ClassificationRequest,
    NoulResult,
    NoulSpec,
    classify,
)

from arcllm import ArcLLMClassifierError, ArcLLMClassifierUnavailableError
from arcllm.modules.telemetry_budget import clear_budgets
from arcllm.vault import VaultResolver

MODEL = "jev-1.13"
API_KEY = "tsk_live_SENTINEL_KEY_9f8e7d6c"
ENV_KEY = "tsk_env_SENTINEL_KEY_1a2b3c4d"
STATE_MARKER = "SENTINEL-STATE-7731"
STATE = f"Acme renewal closes at $42k/yr, net-60. {STATE_MARKER}"
PINNED_BASE_URL = "https://api.typesafe.ai"
ATTACKER_BASE_URL = "https://attacker.example.test"
SCOPE_OPTIONS = ("company", "personal", "agent_only", "unclear")


def _request(state: str = STATE) -> ClassificationRequest:
    return ClassificationRequest(
        state=state,
        questions={
            "scope": ChoiceSpec(
                instructions="Who is this remembered fact or method useful to?",
                criteria={
                    "company": {
                        "what": "The business: operations, deals, market, reusable processes.",
                        "not_for": "The operator's private life or personal side projects.",
                    },
                    "personal": {
                        "what": "The operator's personal life or personal side projects.",
                        "not_for": "Company deals or company processes.",
                    },
                    "agent_only": {"what": "Housekeeping only this one assistant needs."},
                    "unclear": {"what": "None of these, or not enough information to tell."},
                },
            ),
            "personal_check": NoulSpec(
                instructions="This text is about the operator's personal life or personal projects."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Fake typesafe_sdk — verified 0.7.2 surface, env fallback, body logging
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class _ChoiceAnswer:
    choice: str
    confidence: float
    probabilities: dict[str, float]
    type: str = "choice"


@dataclass(frozen=True)
class _NoulAnswer:
    noul: float
    type: str = "noul"


@dataclass(frozen=True)
class _SystemOneResponse:
    model: str
    usage: _Usage
    answers: dict[str, Any]
    request_id: str | None


def _good_response(model: str = MODEL) -> _SystemOneResponse:
    return _SystemOneResponse(
        model=model,
        usage=_Usage(input_tokens=57, output_tokens=0),
        answers={
            "scope": _ChoiceAnswer(
                choice="company",
                confidence=0.98,
                probabilities={
                    "company": 0.985,
                    "personal": 0.005,
                    "agent_only": 0.005,
                    "unclear": 0.005,
                },
            ),
            "personal_check": _NoulAnswer(noul=0.03),
        },
        request_id="req_jev_001",
    )


class _TypeSafeAPIError(Exception):
    def __init__(self, status: int, request_id: str | None = None) -> None:
        self.status = status
        self.request_id = request_id
        super().__init__(f"TypeSafe API error {status} (request {request_id})")


class _RetryPolicy:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.max_retries = kwargs.get("max_retries")


class _Choice:
    def __init__(self, *, instructions: str, criteria: Any, **extra: Any) -> None:
        self.instructions = instructions
        self.criteria = criteria
        self.extra = extra


class _Noul:
    def __init__(self, *, instructions: str, criteria: Any = None, **extra: Any) -> None:
        self.instructions = instructions
        self.criteria = criteria
        self.extra = extra


def _plain(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _question_to_wire(question: Any) -> dict[str, Any]:
    if isinstance(question, dict):
        return _plain(question)
    if isinstance(question, _Choice):
        return {
            "type": "choice",
            "instructions": question.instructions,
            "criteria": _plain(question.criteria),
        }
    if isinstance(question, _Noul):
        wire: dict[str, Any] = {"type": "noul", "instructions": question.instructions}
        if question.criteria is not None:
            wire["criteria"] = _plain(question.criteria)
        return wire
    raise TypeError(f"fake SDK cannot serialize question {question!r}")


@dataclass
class _FakeSdk:
    """Records every client + call; configurable response / error / hang."""

    clients: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    response: Any = field(default_factory=_good_response)
    error: BaseException | None = None
    hang: bool = False

    def module(self) -> types.ModuleType:
        sdk = self
        body_log = logging.getLogger("typesafe_sdk")

        class AsyncTypeSafeClient:
            def __init__(
                self,
                *,
                api_key: str | None = None,
                model: str | None = None,
                retry: Any = None,
                timeout: Any = None,
                headers: Any = None,
                transport: Any = None,
                http_client: Any = None,
                base_url: str | None = None,
            ) -> None:
                self.kwargs = {
                    "api_key": api_key,
                    "model": model,
                    "retry": retry,
                    "timeout": timeout,
                    "headers": headers,
                    "transport": transport,
                    "http_client": http_client,
                    "base_url": base_url,
                }
                # Real SDK behaviour: ambient env only when the argument is None.
                self.effective_api_key = (
                    api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY")
                )
                self.effective_base_url = (
                    base_url
                    if base_url is not None
                    else os.environ.get("TYPESAFE_BASE_URL", PINNED_BASE_URL)
                )
                self.effective_model = (
                    model
                    if model is not None
                    else os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest")
                )
                self.closed = False
                sdk.clients.append(self)

            async def __aenter__(self) -> AsyncTypeSafeClient:
                return self

            async def __aexit__(self, *_exc: object) -> None:
                await self.aclose()

            async def aclose(self) -> None:
                self.closed = True

            async def close(self) -> None:
                self.closed = True

            async def system_one(
                self, state: Any, questions: dict[str, Any], **kwargs: Any
            ) -> Any:
                wire = {
                    "model": kwargs.get("model") or self.effective_model,
                    "state": state,
                    "questions": {k: _question_to_wire(q) for k, q in questions.items()},
                }
                sdk.calls.append(
                    {
                        "wire": wire,
                        "base_url": self.effective_base_url,
                        "authorization": f"Bearer {self.effective_api_key}",
                    }
                )
                # The real SDK logs request/response BODIES at debug.
                body_log.debug("POST /v1/systemone request body: %s", json.dumps(wire))
                if sdk.hang:
                    await asyncio.sleep(3600)
                if sdk.error is not None:
                    raise sdk.error
                body_log.debug("POST /v1/systemone response body: %r", sdk.response)
                return sdk.response

        module = types.ModuleType("typesafe_sdk")
        module.AsyncTypeSafeClient = AsyncTypeSafeClient  # type: ignore[attr-defined]  # reason: dynamic fake module
        module.Choice = _Choice  # type: ignore[attr-defined]  # reason: dynamic fake module
        module.Noul = _Noul  # type: ignore[attr-defined]  # reason: dynamic fake module
        module.RetryPolicy = _RetryPolicy  # type: ignore[attr-defined]  # reason: dynamic fake module
        module.TypeSafeAPIError = _TypeSafeAPIError  # type: ignore[attr-defined]  # reason: dynamic fake module
        module.SystemOneResponse = _SystemOneResponse  # type: ignore[attr-defined]  # reason: dynamic fake module
        module.ChoiceAnswer = _ChoiceAnswer  # type: ignore[attr-defined]  # reason: dynamic fake module
        module.NoulAnswer = _NoulAnswer  # type: ignore[attr-defined]  # reason: dynamic fake module
        module.Usage = _Usage  # type: ignore[attr-defined]  # reason: dynamic fake module
        return module


class _VaultBackend:
    def __init__(self, secrets: dict[str, str], *, available: bool = True) -> None:
        self._secrets = secrets
        self._available = available
        self.lookups: list[str] = []

    def get_secret(self, path: str) -> str | None:
        self.lookups.append(path)
        return self._secrets.get(path)

    def is_available(self) -> bool:
        return self._available


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "arcstore"))
    for name in ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    sdk_logger = logging.getLogger("typesafe_sdk")
    saved = (
        sdk_logger.level,
        sdk_logger.disabled,
        sdk_logger.propagate,
        list(sdk_logger.filters),
        list(sdk_logger.handlers),
    )
    clear_budgets()
    yield
    clear_budgets()
    (
        sdk_logger.level,
        sdk_logger.disabled,
        sdk_logger.propagate,
        sdk_logger.filters,
        sdk_logger.handlers,
    ) = saved[0], saved[1], saved[2], saved[3], saved[4]


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> _FakeSdk:
    sdk = _FakeSdk()
    monkeypatch.setitem(sys.modules, "typesafe_sdk", sdk.module())
    return sdk


def _vault_resolver(key: str = API_KEY, *, available: bool = True) -> VaultResolver:
    return VaultResolver(_VaultBackend({"secret/typesafe": key}, available=available))


def _plugin(**overrides: Any) -> JevClassifier:
    kwargs: dict[str, Any] = {
        "model": MODEL,
        "vault_resolver": _vault_resolver(),
        "vault_path": "secret/typesafe",
    }
    kwargs.update(overrides)
    return JevClassifier(**kwargs)


async def _run(plugin: JevClassifier, **kwargs: Any) -> Any:
    kwargs.setdefault("telemetry", {"arcstore_enabled": False})
    kwargs.setdefault("timeout", 5.0)
    return await classify(provider=plugin, model=MODEL, request=_request(), **kwargs)


def _exception_chain_text(error: BaseException) -> str:
    seen: list[str] = []
    current: BaseException | None = error
    while current is not None and len(seen) < 10:
        seen.append(str(current))
        seen.append(repr(current))
        current = current.__cause__ or current.__context__
    return "\n".join(seen)


def _spooled_bytes(tmp_path: Path) -> bytes:
    root = tmp_path / "arcstore"
    if not root.exists():
        return b""
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())


# ---------------------------------------------------------------------------
# Request shape (fake SDK)
# ---------------------------------------------------------------------------


async def test_jev_sends_pinned_model_state_and_both_questions(fake_sdk: _FakeSdk) -> None:
    await _run(_plugin())

    assert len(fake_sdk.calls) == 1
    wire = fake_sdk.calls[0]["wire"]
    assert wire["model"] == MODEL
    assert wire["state"] == STATE
    assert set(wire["questions"]) == {"scope", "personal_check"}
    scope = wire["questions"]["scope"]
    assert scope["type"] == "choice"
    assert scope["instructions"] == "Who is this remembered fact or method useful to?"
    assert set(scope["criteria"]) == set(SCOPE_OPTIONS)
    check = wire["questions"]["personal_check"]
    assert check["type"] == "noul"
    assert check["instructions"].startswith("This text is about the operator's personal life")


async def test_jev_passes_key_base_url_and_model_explicitly(
    fake_sdk: _FakeSdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ambient TYPESAFE_* env must never steer the client: a hostile
    TYPESAFE_BASE_URL would exfiltrate memory text to an attacker host."""
    monkeypatch.setenv("TYPESAFE_BASE_URL", ATTACKER_BASE_URL)
    monkeypatch.setenv("TYPESAFE_DEFAULT_MODEL", "jev-latest")

    await _run(_plugin())

    client = fake_sdk.clients[0]
    assert client.kwargs["api_key"] == API_KEY, "key must be passed explicitly, not via env"
    assert client.kwargs["base_url"] == PINNED_BASE_URL
    assert client.effective_base_url == PINNED_BASE_URL
    assert fake_sdk.calls[0]["base_url"] == PINNED_BASE_URL
    assert fake_sdk.calls[0]["wire"]["model"] == MODEL
    assert fake_sdk.calls[0]["authorization"] == f"Bearer {API_KEY}"


async def test_jev_forwards_transport_and_bounds_retries_and_timeout(
    fake_sdk: _FakeSdk,
) -> None:
    sentinel_transport = object()

    await _run(_plugin(transport=sentinel_transport, timeout=7.5))

    kwargs = fake_sdk.clients[0].kwargs
    assert kwargs["transport"] is sentinel_transport
    assert kwargs["timeout"] is not None
    retry = kwargs["retry"]
    assert isinstance(retry, _RetryPolicy), "retries must be explicitly bounded"
    assert retry.max_retries is not None and 0 <= retry.max_retries <= 2


# ---------------------------------------------------------------------------
# Response mapping
# ---------------------------------------------------------------------------


async def test_jev_maps_response_to_classification_result(fake_sdk: _FakeSdk) -> None:
    result = await _run(_plugin())

    assert result.model == MODEL
    assert result.request_id == "req_jev_001"
    assert result.input_tokens == 57
    scope = result.answers["scope"]
    assert isinstance(scope, ChoiceResult)
    assert scope.choice == "company"
    assert scope.confidence == pytest.approx(0.98)
    assert dict(scope.probabilities) == pytest.approx(
        {"company": 0.985, "personal": 0.005, "agent_only": 0.005, "unclear": 0.005}
    )
    check = result.answers["personal_check"]
    assert isinstance(check, NoulResult)
    assert check.noul == pytest.approx(0.03)


async def test_jev_reports_the_model_the_server_answered_with(fake_sdk: _FakeSdk) -> None:
    """The verdict records the version that actually answered (REQ-491), so a
    silently re-routed model can be detected downstream."""
    fake_sdk.response = _good_response(model="jev-1.14")

    result = await _run(_plugin())

    assert result.model == "jev-1.14"


# ---------------------------------------------------------------------------
# Key resolution — VaultResolver: vault path first, then TYPESAFE_API_KEY
# ---------------------------------------------------------------------------


async def test_jev_prefers_vault_key_over_env(
    fake_sdk: _FakeSdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", ENV_KEY)

    await _run(_plugin())

    assert fake_sdk.clients[0].kwargs["api_key"] == API_KEY


async def test_jev_falls_back_to_env_key_when_vault_unavailable(
    fake_sdk: _FakeSdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", ENV_KEY)

    await _run(_plugin(vault_resolver=_vault_resolver(available=False)))

    assert fake_sdk.clients[0].kwargs["api_key"] == ENV_KEY


async def test_jev_env_only_default_resolver(
    fake_sdk: _FakeSdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", ENV_KEY)

    await _run(JevClassifier(model=MODEL))

    assert fake_sdk.clients[0].kwargs["api_key"] == ENV_KEY


async def test_jev_resolves_key_per_call_so_rotation_takes_effect(
    fake_sdk: _FakeSdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = JevClassifier(model=MODEL)
    monkeypatch.setenv("TYPESAFE_API_KEY", ENV_KEY)
    await _run(plugin)
    monkeypatch.setenv("TYPESAFE_API_KEY", API_KEY)
    await _run(plugin)

    assert [c.kwargs["api_key"] for c in fake_sdk.clients] == [ENV_KEY, API_KEY]


async def test_jev_does_not_export_vault_key_into_process_env(fake_sdk: _FakeSdk) -> None:
    """Passing the key via os.environ would hand it to every child process."""
    await _run(_plugin())

    assert os.environ.get("TYPESAFE_API_KEY") is None
    assert API_KEY not in "\n".join(os.environ.values())


async def test_jev_without_any_key_is_unavailable_and_sends_nothing(fake_sdk: _FakeSdk) -> None:
    with pytest.raises(ArcLLMClassifierUnavailableError):
        await _run(JevClassifier(model=MODEL))

    assert fake_sdk.clients == []
    assert fake_sdk.calls == []


# ---------------------------------------------------------------------------
# SDK absent — typed unavailable, never ImportError
# ---------------------------------------------------------------------------


async def test_jev_with_sdk_import_blocked_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)

    with pytest.raises(ArcLLMClassifierUnavailableError):
        await _run(_plugin())


def test_jev_module_imports_and_constructs_with_sdk_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK is imported lazily per call, never at plugin import/construct time."""
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)
    monkeypatch.delitem(sys.modules, "arcllm.classifiers.jev", raising=False)

    module = importlib.import_module("arcllm.classifiers.jev")
    plugin = module.JevClassifier(model=MODEL)

    assert plugin.model_name == MODEL


# ---------------------------------------------------------------------------
# Error mapping — every failure is ArcLLMClassifierError, key never in the text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 422, 429, 529])
async def test_jev_http_error_status_maps_to_classifier_error(
    fake_sdk: _FakeSdk, status: int
) -> None:
    fake_sdk.error = _TypeSafeAPIError(status, "req_err")

    with pytest.raises(ArcLLMClassifierError) as info:
        await _run(_plugin())

    assert not isinstance(info.value, ArcLLMClassifierUnavailableError)
    assert API_KEY not in _exception_chain_text(info.value)


@pytest.mark.parametrize(
    "error",
    [TimeoutError("read timed out"), OSError("connection refused")],
    ids=["timeout", "connection"],
)
async def test_jev_transport_failure_maps_to_classifier_error(
    fake_sdk: _FakeSdk, error: BaseException
) -> None:
    fake_sdk.error = error

    with pytest.raises(ArcLLMClassifierError) as info:
        await _run(_plugin())

    assert API_KEY not in _exception_chain_text(info.value)


async def test_jev_hanging_call_is_bounded_by_timeout(fake_sdk: _FakeSdk) -> None:
    fake_sdk.hang = True
    started = time.monotonic()

    with pytest.raises(ArcLLMClassifierError):
        await _run(_plugin(timeout=0.05), timeout=0.05)

    assert time.monotonic() - started < 2.0


def _bad(**changes: Any) -> Any:
    good = _good_response()
    answers = dict(good.answers)
    answers.update(changes.pop("answers", {}))
    for key in changes.pop("drop", ()):
        answers.pop(key)
    return _SystemOneResponse(
        model=good.model, usage=good.usage, answers=answers, request_id=good.request_id
    )


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(_bad(drop=("scope",)), id="missing-scope-answer"),
        pytest.param(_bad(drop=("personal_check",)), id="missing-noul-answer"),
        pytest.param(_bad(answers={"scope": _NoulAnswer(noul=0.9)}), id="scope-answered-as-noul"),
        pytest.param(
            _bad(
                answers={
                    "scope": _ChoiceAnswer(
                        choice="banana", confidence=0.99, probabilities={"banana": 1.0}
                    )
                }
            ),
            id="choice-not-among-options",
        ),
        pytest.param(
            _bad(
                answers={
                    "scope": _ChoiceAnswer(
                        choice="company",
                        confidence=math.nan,
                        probabilities={
                            "company": math.nan,
                            "personal": 0.0,
                            "agent_only": 0.0,
                            "unclear": 0.0,
                        },
                    )
                }
            ),
            id="non-finite-probability",
        ),
        pytest.param(None, id="null-response"),
        pytest.param({"unexpected": "shape"}, id="wrong-type-response"),
    ],
)
async def test_jev_malformed_response_maps_to_classifier_error(
    fake_sdk: _FakeSdk, response: Any
) -> None:
    fake_sdk.response = response

    with pytest.raises(ArcLLMClassifierError):
        await _run(_plugin())


# ---------------------------------------------------------------------------
# Leak prevention — key and state never in logs / telemetry
# ---------------------------------------------------------------------------


async def test_sdk_body_logging_is_silenced_and_key_never_logged(
    fake_sdk: _FakeSdk, caplog: pytest.LogCaptureFixture
) -> None:
    """An operator running with root DEBUG must not get memory text (or the
    key) dumped into logs by the SDK's request/response body logging."""
    caplog.set_level(logging.DEBUG)

    await _run(_plugin())

    assert fake_sdk.calls, "the fake SDK was never called — test would be vacuous"
    assert STATE_MARKER not in caplog.text
    assert API_KEY not in caplog.text
    for record in caplog.records:
        assert STATE_MARKER not in record.getMessage()
        assert API_KEY not in record.getMessage()


async def test_key_never_logged_on_failure_paths(
    fake_sdk: _FakeSdk, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake_sdk.error = _TypeSafeAPIError(401, "req_401")

    with pytest.raises(ArcLLMClassifierError):
        await _run(_plugin())

    assert API_KEY not in caplog.text
    assert STATE_MARKER not in caplog.text


async def test_jev_telemetry_never_carries_state_or_key(
    fake_sdk: _FakeSdk, tmp_path: Path
) -> None:
    from arcstore.records import SpoolRecord

    captured: list[SpoolRecord] = []
    await _run(
        _plugin(),
        telemetry={"store_raw_bodies": True, "agent_did": "did:arc:local:executor/t"},
        on_event=captured.append,
    )

    assert len(captured) == 1
    dumped = captured[0].model_dump_json()
    assert STATE_MARKER not in dumped
    assert API_KEY not in dumped
    assert captured[0].model == MODEL
    spooled = _spooled_bytes(tmp_path)
    assert spooled, "classify did not spool its llm_call record"
    assert STATE_MARKER.encode() not in spooled
    assert API_KEY.encode() not in spooled


# ---------------------------------------------------------------------------
# Real SDK at the HTTP wire — skipped unless arcllm[jev] is installed
# ---------------------------------------------------------------------------


def _jev_json(model: str = MODEL) -> dict[str, Any]:
    return {
        "model": model,
        "answers": {
            "scope": {
                "type": "choice",
                "choice": "company",
                "probabilities": {
                    "company": 0.985,
                    "personal": 0.005,
                    "agent_only": 0.005,
                    "unclear": 0.005,
                },
                "confidence": 0.98,
            },
            "personal_check": {"type": "noul", "noul": 0.03},
        },
        "usage": {"input_tokens": 57, "output_tokens": 0},
    }


class TestRealSdkWire:
    """Needs ``typesafe_sdk`` + ``httpx2`` (``arcllm[jev]``); skipped otherwise."""

    @pytest.fixture
    def httpx2(self) -> Any:
        pytest.importorskip("typesafe_sdk")
        return pytest.importorskip("httpx2")

    async def test_wire_request_is_post_systemone_with_bearer_and_body(
        self, httpx2: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TYPESAFE_BASE_URL", ATTACKER_BASE_URL)
        monkeypatch.setenv("TYPESAFE_API_KEY", ENV_KEY)
        seen: list[Any] = []

        def handler(request: Any) -> Any:
            seen.append(request)
            return httpx2.Response(200, json=_jev_json())

        result = await _run(_plugin(transport=httpx2.MockTransport(handler)))

        assert len(seen) == 1
        request = seen[0]
        assert request.method == "POST"
        assert request.url.host == "api.typesafe.ai"
        assert request.url.path == "/v1/systemone"
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        body = json.loads(request.content)
        assert body["model"] == MODEL
        assert body["state"] == STATE
        assert body["questions"]["scope"]["type"] == "choice"
        assert set(body["questions"]["scope"]["criteria"]) == set(SCOPE_OPTIONS)
        assert body["questions"]["personal_check"]["type"] == "noul"
        scope = result.answers["scope"]
        assert isinstance(scope, ChoiceResult)
        assert scope.choice == "company"
        assert result.input_tokens == 57

    @pytest.mark.parametrize("status", [401, 422, 429, 529])
    async def test_wire_error_status_is_classifier_error_with_bounded_retries(
        self, httpx2: Any, status: int
    ) -> None:
        seen: list[Any] = []

        def handler(request: Any) -> Any:
            seen.append(request)
            return httpx2.Response(status, json={"error": "nope"})

        with pytest.raises(ArcLLMClassifierError) as info:
            await _run(_plugin(transport=httpx2.MockTransport(handler)), timeout=30.0)

        assert 1 <= len(seen) <= 3
        assert API_KEY not in _exception_chain_text(info.value)

    async def test_wire_malformed_body_is_classifier_error(self, httpx2: Any) -> None:
        def handler(request: Any) -> Any:
            return httpx2.Response(200, content=b"<html>not json</html>")

        with pytest.raises(ArcLLMClassifierError):
            await _run(_plugin(transport=httpx2.MockTransport(handler)))

    async def test_wire_sdk_debug_logging_never_emits_state_or_key(
        self, httpx2: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)

        def handler(request: Any) -> Any:
            return httpx2.Response(200, json=_jev_json())

        await _run(_plugin(transport=httpx2.MockTransport(handler)))

        assert STATE_MARKER not in caplog.text
        assert API_KEY not in caplog.text
