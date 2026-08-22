"""RoutingModule — the always-on entry point to every arcllm call.

Every ``load_model`` returns a router. With one declared route it is a
pass-through that costs a dict lookup; with several it picks one per call and
dispatches to that route's adapter.

Selection is a fixed four-tier ladder, highest first:

1. **Pin** — the caller passed ``route="..."``. A background job that must not
   be guessed at (memory consolidation, evals) says so explicitly.
2. **Tool continuity** — the tail of the conversation answers a tool call this
   router already dispatched. The result MUST go back to the model that asked
   for it, so the lock wins over every preference below it. This is a
   correctness boundary, not a policy.
3. **Phrases** — semantic match of the last user message against each route's
   example phrases, above a configured similarity threshold.
4. **Default** — the declared default route.

Tier 2 is what makes tier 3 affordable. An agent turn is one user message
followed by ten to twenty-five tool round-trips; only the first call is
unlocked, so the embedding is paid once per turn rather than once per call.

The router learns tool ownership from its own dispatches: every tool-call id in
a response is recorded against the route that produced it, and a later
``tool_result`` naming that id resolves back to the same route. Ids are unique
per call, so concurrent sessions sharing one router cannot collide, and nothing
here needs to know that a caller's loop, run, or session exists.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from opentelemetry import trace

from arcllm.exceptions import ArcLLMConfigError, ArcLLMEmbeddingUnavailableError
from arcllm.modules.base import resolve_enforcement, validate_config_keys
from arcllm.types import (
    Delta,
    LLMProvider,
    LLMResponse,
    Message,
    ResponseFormat,
    TextBlock,
    Tool,
    ToolResultBlock,
)

logger = logging.getLogger(__name__)

# Route names follow the same safety rules as budget scopes: lowercase
# alphanumeric plus underscores/colons/dots/hyphens, max 128 chars.
_ROUTE_NAME_RE = re.compile(r"^[a-z][a-z0-9_:.\-]{0,127}$")

#: How many tool-call ids the continuity lock remembers. A turn holds at most a
#: few dozen open ids and they are consumed within seconds, so this is a
#: generous ceiling whose only job is to stop unbounded growth in a fleet
#: process that never restarts.
_DEFAULT_LOCK_CAPACITY = 4096

VALID_CONFIG_KEYS = {
    "enabled",
    "enforcement",
    "default_route",
    "routes",
    "threshold",
    "embedding_model",
    "embedding_backend",
    "embedding_base_url",
    "on_embedder_error",
    "lock_capacity",
    "classification",
    "residency",
    "allowed_routes",
    "required_capabilities",
    "remaining_budget_usd",
    "policy_version",
}

_CLASSIFICATION_RANK = {
    "unclassified": 0,
    "cui": 1,
    "confidential": 2,
    "secret": 3,
    "top_secret": 4,
}


@dataclass(frozen=True)
class Route:
    """One reachable provider+model, and the phrases that select it.

    ``phrases`` are example utterances, not patterns. An empty tuple means the
    route is reachable only by pin or by being the default — which is the right
    shape for a fallback lane nobody should land on by accident.
    """

    name: str
    provider: str
    model: str | None = None
    phrases: tuple[str, ...] = ()
    classification_max: str = "top_secret"
    residency: str | None = None
    capabilities: tuple[str, ...] = ("tools", "streaming")
    cost_per_1k: float = 0.0
    latency_ms: float = 0.0

    def __post_init__(self) -> None:
        if not _ROUTE_NAME_RE.match(self.name):
            raise ArcLLMConfigError(
                f"Invalid route name {self.name!r}. Must be lowercase alphanumeric "
                "with underscores, colons, dots, or hyphens, max 128 characters."
            )
        if not self.provider:
            raise ArcLLMConfigError(f"Route {self.name!r} is missing 'provider'")
        if self.classification_max.lower() not in _CLASSIFICATION_RANK:
            raise ArcLLMConfigError(
                f"Unknown route classification_max {self.classification_max!r}"
            )
        if self.cost_per_1k < 0 or self.latency_ms < 0:
            raise ArcLLMConfigError("route cost_per_1k and latency_ms must be non-negative")

    @property
    def label(self) -> str:
        """``provider/model`` as an operator would write it in config."""
        return f"{self.provider}/{self.model}" if self.model else self.provider


def parse_routes(
    routes_config: Any,
    *,
    default_provider: str,
    default_model: str | None,
    default_name: str = "default",
) -> list[Route]:
    """Build the route table from config plus the caller's own default.

    ``routes_config`` is the ``[modules.routing.routes]`` table: route name ->
    ``{model = "provider/model", phrases = [...]}``. A route writes its target
    as one ``provider/model`` string, exactly the way an agent already writes
    ``[llm] model``, so there is one spelling to learn and one parser.

    The provider and model handed to ``load_model`` always become a route
    (named ``default_name``) unless config declares one under that name, so a
    caller's existing default is never lost by adding alternates.
    """
    routes: list[Route] = []
    declared = routes_config or {}
    if not isinstance(declared, dict):
        raise ArcLLMConfigError(
            "routing 'routes' must be a table of route-name -> settings, "
            f"got {type(declared).__name__}"
        )

    for name, settings in declared.items():
        if not isinstance(settings, dict):
            raise ArcLLMConfigError(
                f"Route {name!r} must be a table, got {type(settings).__name__}"
            )
        target = settings.get("model")
        if not target:
            raise ArcLLMConfigError(f"Route {name!r} is missing 'model' (\"provider/model\")")
        provider, _, model = str(target).partition("/")
        phrases = settings.get("phrases", [])
        if not isinstance(phrases, list) or any(not isinstance(p, str) for p in phrases):
            raise ArcLLMConfigError(f"Route {name!r} 'phrases' must be a list of strings")
        routes.append(
            Route(
                name=name,
                provider=provider,
                model=model or None,
                phrases=tuple(phrases),
                classification_max=str(settings.get("classification_max", "top_secret")),
                residency=settings.get("residency"),
                capabilities=tuple(settings.get("capabilities", ("tools", "streaming"))),
                cost_per_1k=float(settings.get("cost_per_1k", 0.0)),
                latency_ms=float(settings.get("latency_ms", 0.0)),
            )
        )

    if not any(r.name == default_name for r in routes):
        routes.insert(
            0,
            Route(name=default_name, provider=default_provider, model=default_model),
        )
    return routes


@dataclass
class Decision:
    """Which route a call took and why — the payload of every routing span."""

    route: str
    reason: str
    score: float | None = None
    runner_up: str | None = None
    request_hash: str | None = None
    policy_version: str | None = None


@dataclass(frozen=True)
class RoutingRequest:
    """Non-sensitive routing facts supplied to a policy."""

    classification: str = "unclassified"
    residency: str | None = None
    allowed_routes: frozenset[str] | None = None
    required_capabilities: frozenset[str] = frozenset()
    remaining_budget_usd: float | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class RoutingDecision:
    """A policy result containing labels and hashes, never prompt content."""

    route: str
    reason: str
    request_hash: str | None = None
    policy_version: str = "native-v1"


class RoutingPolicy(Protocol):
    """Selects a route; provider invocation remains owned by RoutingModule."""

    async def decide(
        self, request: RoutingRequest, targets: Sequence[Route]
    ) -> RoutingDecision: ...


@dataclass
class _PhraseIndex:
    """Route phrases as unit vectors, built once and reused."""

    routes: tuple[str, ...] = ()
    vectors: tuple[tuple[float, ...], ...] = ()
    built: bool = False
    disabled: bool = False


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Dot product of two unit vectors.

    ``embed`` normalizes, so the dot product *is* cosine similarity and the
    magnitudes never need recomputing.
    """
    return sum(x * y for x, y in zip(a, b, strict=True))


def _last_user_text(messages: Sequence[Message]) -> str | None:
    """Text of the newest genuine user message, or None if there is none.

    Tool results also arrive on the ``user`` role in the neutral message shape,
    so a message whose content is a block list containing a ``tool_result`` is
    machine output and is skipped. Only prose the operator or a teammate
    actually wrote can steer a route.
    """
    for message in reversed(messages):
        if message.role != "user":
            continue
        content = message.content
        if isinstance(content, str):
            return content or None
        if _tool_result_ids([message]):
            continue
        text = " ".join(b.text for b in content if isinstance(b, TextBlock))
        if text.strip():
            return text
    return None


def _tool_result_ids(messages: Iterable[Message]) -> list[str]:
    """Every ``tool_use_id`` answered by the given messages."""
    return [
        block.tool_use_id
        for message in messages
        if not isinstance(message.content, str)
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]


def _trailing_tool_result_ids(messages: Sequence[Message]) -> list[str]:
    """Tool-call ids answered by the unbroken run of tool results at the tail.

    Walks backwards while each message is answering tool calls and stops at the
    first message that is not. Parallel dispatch can append several result
    messages at once, so the run may be longer than one. Anything earlier in
    the history belongs to a cycle that already closed and must not lock
    anything.
    """
    ids: list[str] = []
    for message in reversed(messages):
        answered = _tool_result_ids([message])
        if not answered:
            break
        ids.extend(answered)
    return ids


class RoutingModule(LLMProvider):
    """Selects a route per call and dispatches to that route's adapter.

    Adapters are built lazily through ``build_adapter``: declaring four models
    opens connection pools only for the ones actually reached.
    """

    def __init__(
        self,
        config: dict[str, Any],
        routes: Sequence[Route],
        build_adapter: Callable[[Route], LLMProvider],
        policy: RoutingPolicy | None = None,
    ) -> None:
        validate_config_keys(config, VALID_CONFIG_KEYS, "RoutingModule")
        if not routes:
            raise ArcLLMConfigError("RoutingModule requires at least one route")

        names = [r.name for r in routes]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ArcLLMConfigError(f"Duplicate route names: {sorted(duplicates)}")

        self._routes: dict[str, Route] = {r.name: r for r in routes}
        self._build_adapter = build_adapter
        self._policy = policy
        self._default_classification = str(config.get("classification", "unclassified")).lower()
        self._default_residency = config.get("residency")
        self._default_allowed_routes = (
            frozenset(config["allowed_routes"]) if config.get("allowed_routes") else None
        )
        self._default_required_capabilities = frozenset(config.get("required_capabilities", ()))
        self._default_remaining_budget = config.get("remaining_budget_usd")
        self._enforcement = resolve_enforcement(config)

        self._default = config.get("default_route") or names[0]
        if self._default not in self._routes:
            raise ArcLLMConfigError(
                f"default_route {self._default!r} is not a declared route: {sorted(self._routes)}"
            )

        self._threshold = float(config.get("threshold", 0.45))
        if not 0.0 <= self._threshold <= 1.0:
            raise ArcLLMConfigError(f"threshold must be between 0 and 1, got {self._threshold}")

        self._embedding_model: str = config.get("embedding_model") or "all-MiniLM-L6-v2"
        self._embedding_backend: str = config.get("embedding_backend") or "local"
        self._embedding_base_url: str | None = config.get("embedding_base_url") or None

        # Fail closed by default. A deployment that configured phrases and then
        # lost its embedder is not "routing to the default" — it is silently not
        # routing at all, which is the failure mode that hides longest.
        self._on_embedder_error: str = config.get("on_embedder_error", "raise")
        if self._on_embedder_error not in ("raise", "default_route"):
            raise ArcLLMConfigError(
                "on_embedder_error must be 'raise' or 'default_route', "
                f"got {self._on_embedder_error!r}"
            )

        self._lock_capacity = int(config.get("lock_capacity", _DEFAULT_LOCK_CAPACITY))
        self._tool_routes: OrderedDict[tuple[str, str] | str, str] = OrderedDict()
        self._tool_lock = threading.Lock()

        self._adapters: dict[str, LLMProvider] = {}
        self._adapter_lock = threading.Lock()

        self._index = _PhraseIndex()
        self._index_error: ArcLLMConfigError | None = None
        self._tracer = trace.get_tracer("arcllm")

        # One declared route means there is nothing to decide, so the whole
        # ladder — and the span that would record its reasoning — is skipped.
        # Being always-on must cost a deployment that never configured a second
        # model exactly nothing, or "always-on" becomes a tax on the default.
        # An explicit pin still takes the full path even here: a caller naming a
        # route it was not granted deserves the same answer whether the agent
        # happens to have one model or five.
        self._single: str | None = self._default if len(self._routes) == 1 else None

        # The default route is built now; alternates wait until something
        # selects them. Deferring the default too would move a misspelled
        # provider or a missing adapter module out of construction and into the
        # middle of the first turn, trading a startup error for a runtime one.
        # Alternates keep the laziness because their failure surfaces the first
        # time they are chosen, which is the earliest it can.
        self.adapter_for(self._default)

    # -- identity ----------------------------------------------------------

    @property
    def name(self) -> str:
        """Provider of the default route."""
        return self._routes[self._default].provider

    @property
    def model_name(self) -> str:
        """Model of the default route.

        The router has no single model, so this reports the lane a call takes
        when nothing selects otherwise. Per-call truth lives on the response
        and in the routing span, never here.
        """
        route = self._routes[self._default]
        return route.model or route.provider

    @property
    def routes(self) -> tuple[str, ...]:
        """Declared route names, in declaration order."""
        return tuple(self._routes)

    # -- adapters ----------------------------------------------------------

    def adapter_for(self, name: str) -> LLMProvider:
        """Return the adapter serving route ``name``, building it on first use."""
        if name not in self._routes:
            raise ArcLLMConfigError(
                f"Unknown route {name!r}. Declared routes: {sorted(self._routes)}"
            )
        cached = self._adapters.get(name)
        if cached is not None:
            return cached
        with self._adapter_lock:
            cached = self._adapters.get(name)
            if cached is None:
                cached = self._build_adapter(self._routes[name])
                self._adapters[name] = cached
        return cached

    # -- continuity lock ---------------------------------------------------

    def _remember_tool_calls(
        self, ids: Iterable[str], route: str, session_id: str = "default"
    ) -> None:
        """Record that ``route`` asked for these tool calls."""
        with self._tool_lock:
            for tool_id in ids:
                key: tuple[str, str] | str = (
                    tool_id if session_id == "default" else (session_id, tool_id)
                )
                self._tool_routes[key] = route
                self._tool_routes.move_to_end(key)
            while len(self._tool_routes) > self._lock_capacity:
                self._tool_routes.popitem(last=False)

    def _locked_route(
        self, messages: Sequence[Message], session_id: str = "default"
    ) -> str | None:
        """The route owed this call's tool results, or None if no cycle is open."""
        answered = _trailing_tool_result_ids(messages)
        if not answered:
            return None
        with self._tool_lock:
            for tool_id in answered:
                route = self._tool_routes.get(
                    tool_id if session_id == "default" else (session_id, tool_id)
                )
                if route is not None and route in self._routes:
                    return route
        return None

    # -- phrase matching ---------------------------------------------------

    def _embedder(self) -> Any:
        """The configured embedding backend.

        ``resolve_embedder`` caches local models per name, so this is a lookup
        after the first call rather than a reload of the weights.
        """
        from arcllm.embeddings import resolve_embedder

        return resolve_embedder(
            self._embedding_model,
            backend=self._embedding_backend,
            base_url=self._embedding_base_url,
        )

    async def _build_index(self) -> _PhraseIndex:
        """Embed every route's phrases and return the finished index."""
        pairs = [(r.name, phrase) for r in self._routes.values() for phrase in r.phrases]
        if not pairs:
            return _PhraseIndex(built=True)

        response = await self._embedder().embed([p for _, p in pairs])
        return _PhraseIndex(
            routes=tuple(name for name, _ in pairs),
            vectors=tuple(tuple(v) for v in response.vectors),
            built=True,
        )

    async def _ensure_index(self) -> bool:
        """Build the phrase index if needed. Returns False when unusable.

        The finished index is swapped in as one object. Assigning its route
        names and its vectors separately would let a concurrent first call read
        a half-built pair and zip two different lengths.
        """
        if self._index_error is not None:
            raise self._index_error
        index = self._index
        if index.disabled:
            return False
        if index.built:
            return bool(index.vectors)
        try:
            self._index = await self._build_index()
        except (ArcLLMEmbeddingUnavailableError, ImportError, OSError) as exc:
            if self._on_embedder_error == "raise":
                # Remembered, not retried. Fail-closed has to stay closed, and
                # re-attempting a model load on every call turns one broken
                # config into a per-call stall.
                self._index_error = ArcLLMConfigError(
                    f"Semantic routing is configured but the embedder "
                    f"({self._embedding_backend}/{self._embedding_model}) is unavailable: "
                    f"{exc}. Install the 'arcllm[local]' extra, point "
                    "embedding_backend at a reachable endpoint, or set "
                    "on_embedder_error = 'default_route' to accept unrouted calls."
                )
                raise self._index_error from exc
            self._index = _PhraseIndex(built=True, disabled=True)
            logger.error(
                "Semantic routing DISABLED — embedder %s/%s unavailable (%s). "
                "Every call now takes the default route %r.",
                self._embedding_backend,
                self._embedding_model,
                exc,
                self._default,
            )
            return False
        return bool(self._index.vectors)

    async def _match(self, text: str) -> Decision | None:
        """Best route for ``text`` above the threshold, or None."""
        if not await self._ensure_index():
            return None

        index = self._index
        query = (await self._embedder().embed([text])).vectors[0]

        best: dict[str, float] = {}
        for route_name, vector in zip(index.routes, index.vectors, strict=True):
            score = _cosine(query, vector)
            if score > best.get(route_name, -1.0):
                best[route_name] = score

        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
        if not ranked or ranked[0][1] < self._threshold:
            return None
        winner, score = ranked[0]
        return Decision(
            route=winner,
            reason="phrase",
            score=round(score, 4),
            runner_up=ranked[1][0] if len(ranked) > 1 else None,
        )

    # -- selection ---------------------------------------------------------

    def _resolve_pin(self, pin: str) -> str | None:
        """Validate an explicit ``route=`` pin against the declared routes."""
        if not _ROUTE_NAME_RE.match(pin):
            raise ArcLLMConfigError(
                "Invalid route format. Must be lowercase alphanumeric with "
                "underscores, colons, dots, or hyphens, max 128 characters."
            )
        if pin in self._routes:
            return pin
        if self._enforcement == "block":
            raise ArcLLMConfigError(
                f"Unknown route {pin!r}. Declared routes: {sorted(self._routes)}"
            )
        logger.warning("Unknown route %r, falling back to default %r", pin, self._default)
        return None

    async def _select(
        self, messages: Sequence[Message], pin: str | None, eligible: set[str], session_id: str
    ) -> Decision:
        """Run the four-tier ladder and return the winning decision."""
        if pin is not None:
            resolved = self._resolve_pin(pin)
            if resolved is not None:
                return Decision(route=resolved, reason="pinned")

        locked = self._locked_route(messages, session_id)
        if locked is not None and locked in eligible:
            return Decision(route=locked, reason="tool_continuity")

        text = _last_user_text(messages)
        if text is not None:
            matched = await self._match(text)
            if matched is not None and matched.route in eligible:
                return matched

        route = self._default if self._default in eligible else sorted(eligible)[0]
        return Decision(route=route, reason="default")

    def _eligible(self, request: RoutingRequest, tools: list[Tool] | None) -> set[str]:
        """Apply fail-closed authorization, classification, residency and capability gates."""
        level = _CLASSIFICATION_RANK.get(request.classification)
        if level is None:
            raise ArcLLMConfigError(f"Unknown classification {request.classification!r}")
        eligible: set[str] = set()
        for name, route in self._routes.items():
            if request.allowed_routes is not None and name not in request.allowed_routes:
                continue
            if _CLASSIFICATION_RANK.get(route.classification_max, -1) < level:
                continue
            if route.residency is not None and request.residency not in (None, route.residency):
                continue
            required = set(request.required_capabilities)
            if tools:
                required.add("tools")
            if not required.issubset(route.capabilities):
                continue
            if (
                request.remaining_budget_usd is not None
                and route.cost_per_1k > request.remaining_budget_usd
            ):
                continue
            eligible.add(name)
        if not eligible:
            raise ArcLLMConfigError(
                "No route satisfies classification, residency, capability, or budget policy"
            )
        return eligible

    async def _policy_decision(
        self, request: RoutingRequest, eligible: set[str]
    ) -> Decision | None:
        if self._policy is None:
            return None
        request = RoutingRequest(
            classification=request.classification,
            residency=request.residency,
            allowed_routes=frozenset(eligible),
            required_capabilities=request.required_capabilities,
            remaining_budget_usd=request.remaining_budget_usd,
            session_id=request.session_id,
        )
        result = await self._policy.decide(
            request, tuple(route for name, route in self._routes.items() if name in eligible)
        )
        if result.route not in eligible:
            raise ArcLLMConfigError(f"Policy selected unauthorized route {result.route!r}")
        return Decision(
            route=result.route,
            reason=result.reason,
            request_hash=result.request_hash,
            policy_version=result.policy_version,
        )

    def _annotate(self, span: trace.Span, decision: Decision, route: Route) -> None:
        """Put the whole decision on the span — including why, and what lost."""
        span.set_attribute("arcllm.routing.route", decision.route)
        span.set_attribute("arcllm.routing.reason", decision.reason)
        span.set_attribute("arcllm.routing.provider", route.provider)
        span.set_attribute("arcllm.routing.model", route.label)
        span.set_attribute("arcllm.routing.candidates", len(self._routes))
        if decision.score is not None:
            span.set_attribute("arcllm.routing.score", decision.score)
        if decision.runner_up is not None:
            span.set_attribute("arcllm.routing.runner_up", decision.runner_up)
        if decision.request_hash is not None:
            span.set_attribute("arcllm.routing.request_hash", decision.request_hash)
        if decision.policy_version is not None:
            span.set_attribute("arcllm.routing.policy_version", decision.policy_version)

    @staticmethod
    def _stamp(response: LLMResponse, decision: Decision, route: Route) -> None:
        """Record the route on the response so outer modules can price it.

        TelemetryModule sits above the router and would otherwise bill every
        call at the default route's rate. ``arcllm_route_model`` is the
        *configured* id, which is what provider metadata is keyed by — the
        wire ``model`` string often carries a dated suffix that matches nothing.
        """
        metadata = dict(response.metadata or {})
        metadata["arcllm_route"] = decision.route
        metadata["arcllm_route_model"] = route.label
        metadata["arcllm_route_reason"] = decision.reason
        if decision.request_hash is not None:
            metadata["arcllm_route_request_hash"] = decision.request_hash
        if decision.policy_version is not None:
            metadata["arcllm_route_policy_version"] = decision.policy_version
        response.metadata = metadata

    # -- dispatch ----------------------------------------------------------

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        *,
        response_format: ResponseFormat | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Select a route and dispatch, then record the tool calls it made."""
        pin = kwargs.pop("route", None)
        session_id = str(kwargs.pop("session_id", "default"))
        request = RoutingRequest(
            classification=str(kwargs.pop("classification", self._default_classification)).lower(),
            residency=kwargs.pop("residency", self._default_residency),
            allowed_routes=(
                frozenset(kwargs.pop("allowed_routes"))
                if "allowed_routes" in kwargs
                else self._default_allowed_routes
            ),
            required_capabilities=frozenset(
                kwargs.pop("required_capabilities", self._default_required_capabilities)
            ),
            remaining_budget_usd=kwargs.pop(
                "remaining_budget_usd", self._default_remaining_budget
            ),
            session_id=session_id,
        )
        eligible = self._eligible(request, tools)
        locked = self._locked_route(messages, session_id)
        if locked is not None:
            pin = None
            forced = locked
        else:
            forced = None
        if self._single is not None and pin is None and forced is None:
            if self._single not in eligible:
                raise ArcLLMConfigError(
                    f"Selected route {self._single!r} is not permitted by policy"
                )
            return await self.adapter_for(self._single).invoke(
                messages, tools, response_format=response_format, **kwargs
            )
        with self._tracer.start_as_current_span("arcllm.routing") as span:
            decision = (
                Decision(route=forced, reason="tool_continuity")
                if forced
                else await self._policy_decision(request, eligible)
            )
            if decision is None:
                decision = await self._select(messages, pin, eligible, session_id)
            if decision.route not in eligible:
                raise ArcLLMConfigError(
                    f"Selected route {decision.route!r} is not permitted by policy"
                )
            route = self._routes[decision.route]
            self._annotate(span, decision, route)

            response = await self.adapter_for(decision.route).invoke(
                messages, tools, response_format=response_format, **kwargs
            )
            if response.tool_calls:
                self._remember_tool_calls(
                    [tc.id for tc in response.tool_calls], decision.route, session_id
                )
            self._stamp(response, decision, route)
            return response

    async def invoke_stream(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        *,
        response_format: ResponseFormat | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Delta]:
        """Stream from the selected route, learning tool ids as they arrive.

        Overriding this is not optional. The inherited fallback collapses a
        stream into one Delta by calling ``invoke``, so leaving it in place
        would silently cost every routed deployment its token-by-token output.
        """
        pin = kwargs.pop("route", None)
        session_id = str(kwargs.pop("session_id", "default"))
        request = RoutingRequest(
            classification=str(kwargs.pop("classification", self._default_classification)).lower(),
            residency=kwargs.pop("residency", self._default_residency),
            allowed_routes=(
                frozenset(kwargs.pop("allowed_routes"))
                if "allowed_routes" in kwargs
                else self._default_allowed_routes
            ),
            required_capabilities=frozenset(
                kwargs.pop("required_capabilities", self._default_required_capabilities)
            ),
            remaining_budget_usd=kwargs.pop(
                "remaining_budget_usd", self._default_remaining_budget
            ),
            session_id=session_id,
        )
        eligible = self._eligible(request, tools)
        locked = self._locked_route(messages, session_id)
        if locked is not None:
            pin = None
        if self._single is not None and pin is None and locked is None:
            if self._single not in eligible:
                raise ArcLLMConfigError(
                    f"Selected route {self._single!r} is not permitted by policy"
                )
            async for delta in self.adapter_for(self._single).invoke_stream(
                messages, tools, response_format=response_format, **kwargs
            ):
                yield delta
            return
        with self._tracer.start_as_current_span("arcllm.routing") as span:
            decision = (
                Decision(route=locked, reason="tool_continuity")
                if locked
                else await self._policy_decision(request, eligible)
            )
            if decision is None:
                decision = await self._select(messages, pin, eligible, session_id)
            if decision.route not in eligible:
                raise ArcLLMConfigError(
                    f"Selected route {decision.route!r} is not permitted by policy"
                )
            route = self._routes[decision.route]
            self._annotate(span, decision, route)

            seen: list[str] = []
            async for delta in self.adapter_for(decision.route).invoke_stream(
                messages, tools, response_format=response_format, **kwargs
            ):
                if delta.tool_call is not None and delta.tool_call.id:
                    seen.append(delta.tool_call.id)
                yield delta
            if seen:
                self._remember_tool_calls(seen, decision.route, session_id)

    # -- lifecycle ---------------------------------------------------------

    def validate_config(self) -> bool:
        """Validate every declared route, building the adapters it takes to do so.

        Deliberately not limited to routes already built. A caller asking "is
        this configured correctly" wants an answer about the whole table, and a
        vacuous ``True`` from a router that has not been used yet is the kind of
        pass that hides a broken route until the day something selects it.
        """
        return all(self.adapter_for(name).validate_config() for name in self._routes)

    async def close(self) -> None:
        """Close every built adapter, tolerating individual failures."""
        errors: list[Exception] = []
        for name, adapter in self._adapters.items():
            try:
                await adapter.close()
            except Exception as exc:  # reason: fail-open — log + keep closing
                logger.error("Failed to close route %r adapter: %s", name, exc)
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Failed to close some route adapters", errors)
