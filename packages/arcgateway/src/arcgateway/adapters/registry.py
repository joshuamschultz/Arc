"""Adapter registry — the filesystem is the registry (SPEC-065 COMP-003).

A platform is a folder. ``arcgateway/adapters/<name>/`` exporting a module-level
``PLATFORM = AdapterSpec(...)`` is discoverable; deleting the folder deletes the
platform. Nothing here is edited to add one, which is the property that makes
"you can delete it with no problem" literally true (REQ-308, D-670).

Resilience is the security property, not a nicety (REQ-309, ASI04). A folder in
this package is code executed at import time. If a third-party or merely broken
adapter's exception could reach startup, one bad folder would be a denial of
service against the daemon and every other platform on it. So an import failure
is caught, **recorded** as an audit event, and skipped — and the roster that did
load is logged on one line, because a platform that silently stopped working is
otherwise indistinguishable from one nobody configured.

Four Pillars (ADR-019) — tier is *stringency metadata, not a gate*:

    Identity   Each adapter carries the agent DID it serves (ctx.agent_did()).
    Sign       An in-tree folder is code that shipped with the gateway; the
               allowlist of official platform names is the load-time control
               point where Sigstore/arctrust verification will attach.
    Authorize  Platform names are regex-validated (no path traversal /
               injection, ASI04 / NIST SI-10). Unofficial platforms load at
               personal/enterprise with an audit warning but are **blocked** at
               federal (signed-allowlist requirement).
    Audit      Every load / skip / block emits a ``gateway.adapter.*`` event.

Credential-presence gating: a platform whose credentials or optional dependency
are missing raises :class:`AdapterUnavailableError` (or ``ImportError``); the
registry skips it at personal/enterprise but treats it as a hard startup
failure at federal — a federal deployment that enabled an adapter must refuse to
start rather than serve a subset silently.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from arcgateway.audit import emit_event

if TYPE_CHECKING:
    from arcgateway.adapters.base import BasePlatformAdapter, InboundDraft
    from arcgateway.executor import InboundEvent

_logger = logging.getLogger("arcgateway.adapters.registry")

#: Import path of the package scanned for platform folders.
_ADAPTERS_PACKAGE = "arcgateway.adapters"

#: Module-level name a platform folder exports to declare itself.
_DESCRIPTOR = "PLATFORM"

# Platform names: lowercase, start with a letter, max 32 chars. Mirrors
# arcllm's provider-name guard — blocks "../evil", "os.system", etc.
_VALID_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: First-party platforms. At federal tier only these names may load
#: (signed-allowlist control point).
OFFICIAL_ADAPTERS: frozenset[str] = frozenset({"telegram", "slack", "mattermost", "web"})

#: What an adapter calls with each inbound message. A platform adapter hands
#: up a draft (artefacts still on the wire); web/in-process hand up a
#: finished event. ``SessionRouter.handle`` accepts both.
OnMessage = Callable[["InboundEvent | InboundDraft"], Awaitable[None]]


class AdapterUnavailableError(Exception):
    """A platform could not build its adapter (missing credentials/config).

    Adapters raise this (or ``ImportError`` for a missing optional dependency)
    to signal a skippable condition. The registry skips the adapter at
    personal/enterprise tier and re-raises it at federal tier.
    """


def validate_adapter_name(name: str) -> None:
    """Reject platform names that could enable path traversal or injection.

    Args:
        name: Platform name from a ``[platforms.<name>]`` block or a folder.

    Raises:
        ValueError: If ``name`` does not match ``[a-z][a-z0-9_]{0,31}``.
    """
    if not _VALID_NAME_RE.match(name):
        msg = f"invalid adapter name {name!r}; must match [a-z][a-z0-9_]{{0,31}}"
        raise ValueError(msg)


@dataclass(frozen=True)
class AdapterBuildContext:
    """Everything an adapter needs to construct itself.

    Attributes:
        name: The platform name (``[platforms.<name>]`` key).
        raw_config: The raw TOML block for this platform, including ``enabled``
            and an optional ``agent_did`` override. The adapter validates this
            against its own Pydantic model.
        on_message: Async callback the adapter calls with each inbound draft
            (wired to ``SessionRouter.handle`` in production).
        default_agent_did: The gateway-level ``[gateway].agent_did``.
        tier: Deployment tier — ``personal`` | ``enterprise`` | ``federal``.
        require_pairing: ``[security].require_pairing`` — when True, an
            adapter's own static allowlist gate becomes an OR-condition
            rather than the sole gate: a message from a user who fails the
            static check is forwarded to ``on_message`` instead of being
            dropped, so SessionRouter's pairing interceptor can mint/DM a
            pairing code or route an already-approved user through.
    """

    name: str
    raw_config: dict[str, Any]
    on_message: OnMessage
    default_agent_did: str
    tier: str
    require_pairing: bool = False

    def agent_did(self) -> str:
        """Return the DID this adapter serves — block override or gateway default."""
        override = self.raw_config.get("agent_did")
        if isinstance(override, str) and override:
            return override
        return self.default_agent_did


class AdapterBuilder(Protocol):
    """Builds an adapter from a context, or raises if it cannot."""

    def __call__(self, ctx: AdapterBuildContext) -> BasePlatformAdapter: ...


@dataclass(frozen=True)
class AdapterSpec:
    """A platform's self-declaration, exported as ``PLATFORM`` from its folder.

    Attributes:
        name: Platform name (must satisfy :func:`validate_adapter_name`) and the
            folder it lives in.
        requires: Third-party distributions this platform cannot run without.
            Declared rather than discovered so a skip reports *what* is missing
            instead of an ImportError traceback.
        supports: Capabilities the gateway may use — artefact kinds this
            platform can carry outbound, plus optional extras such as ``edit``.
            Undeclared is not the same as unsupported: the gateway has to know
            what a platform cannot carry *before* it tries, or degradation is an
            exception handler and the turn is already at risk.
        build: Validates ``ctx.raw_config``, resolves credentials, and returns
            an adapter — or raises :class:`AdapterUnavailableError` /
            ``ImportError`` when it cannot.
    """

    name: str
    requires: tuple[str, ...]
    supports: tuple[str, ...]
    build: AdapterBuilder


def discover_adapters() -> list[AdapterSpec]:
    """Scan ``arcgateway/adapters/`` for folders exporting a ``PLATFORM``.

    Repeatable: nothing is cached, so a folder added, fixed or deleted between
    two scans is reflected in the next one. A folder that raises on import is
    audited and skipped; the rest of the roster loads.

    Returns:
        Every discovered platform's :class:`AdapterSpec`, in name order.
    """
    specs: list[AdapterSpec] = []
    for info in sorted(pkgutil.iter_modules([str(Path(__file__).parent)]), key=_module_name):
        spec = _load_descriptor(info.name)
        if spec is not None:
            specs.append(spec)

    _logger.info(
        "registry: adapters loaded: %s",
        ", ".join(spec.name for spec in specs) or "none",
    )
    return specs


def _module_name(info: pkgutil.ModuleInfo) -> str:
    return info.name


def _load_descriptor(name: str) -> AdapterSpec | None:
    """Import one candidate module and return its ``PLATFORM``, or None.

    Every ``return None`` here is a *quiet* skip on purpose: the adapters
    package also holds the base contract, this registry and shared helpers, and
    none of them is a platform. Only an import that *failed* is loud, because
    that is a platform someone expected to be there.
    """
    if name.startswith("_"):
        return None
    try:
        validate_adapter_name(name)
    except ValueError:
        _audit("gateway.adapter.blocked", name, "deny", reason="invalid_name")
        _logger.warning("registry: blocked adapter folder with invalid name %r", name)
        return None

    try:
        module = importlib.import_module(f"{_ADAPTERS_PACKAGE}.{name}")
    except Exception as exc:  # reason: one broken folder must not take the daemon down
        _audit("gateway.adapter.blocked", name, "error", reason="import_failed")
        _logger.exception(
            "registry: adapter folder %r failed to import and was skipped: %s", name, exc
        )
        return None

    spec = getattr(module, _DESCRIPTOR, None)
    if spec is None:
        return None
    if not isinstance(spec, AdapterSpec):
        _audit("gateway.adapter.blocked", name, "deny", reason="not_an_adapter_spec")
        _logger.warning("registry: %r exports a %s, not an AdapterSpec", name, type(spec).__name__)
        return None
    if spec.name != name:
        _audit("gateway.adapter.blocked", name, "deny", reason="name_mismatch")
        _logger.warning(
            "registry: folder %r declares itself %r — a platform is named by its folder",
            name,
            spec.name,
        )
        return None
    return spec


def build_adapters(
    *,
    platforms: dict[str, dict[str, Any]],
    on_message: OnMessage,
    default_agent_did: str,
    tier: str,
    require_pairing: bool = False,
) -> list[BasePlatformAdapter]:
    """Build adapters for every enabled platform block, generically.

    Args:
        platforms: ``{name: raw_block}`` for each ``[platforms.<name>]`` other
            than the core ``web`` adapter.
        on_message: Inbound callback wired to ``SessionRouter.handle``.
        default_agent_did: ``[gateway].agent_did``.
        tier: ``personal`` | ``enterprise`` | ``federal``.
        require_pairing: ``[security].require_pairing`` — forwarded to every
            adapter's :class:`AdapterBuildContext` (see its docstring).

    Returns:
        Adapters for each enabled, authorized, buildable platform.

    Raises:
        AdapterUnavailableError: At federal tier, when an enabled platform cannot
            be loaded (folder absent, unofficial, or its build failed) — federal
            fails closed rather than serving a silent subset.
    """
    specs = {spec.name: spec for spec in discover_adapters()}
    is_federal = tier == "federal"
    adapters: list[BasePlatformAdapter] = []

    for name, block in platforms.items():
        if not isinstance(block, dict) or not block.get("enabled"):
            continue

        # A block may set ``platform = "telegram"`` to reuse the telegram adapter
        # under a distinct block name — this is how a fleet runs one bot PER agent
        # (``[platforms.sales_telegram]``, ``[platforms.josh_telegram]``, each with
        # its own token_env + agent_did). Absent the key, the block name IS the
        # platform (unchanged single-bot behavior). Both the block name and the
        # resolved platform name are path-validated.
        platform_name = str(block.get("platform") or name)
        try:
            validate_adapter_name(name)
            validate_adapter_name(platform_name)
        except ValueError:
            _audit("gateway.adapter.blocked", name, "deny", reason="invalid_name")
            _logger.warning("registry: skipping platform with invalid name %r", name)
            if is_federal:
                raise AdapterUnavailableError(f"invalid adapter name {name!r}") from None
            continue

        if platform_name not in OFFICIAL_ADAPTERS:
            # Unofficial platform: blocked at federal, allowed-with-warning otherwise.
            if is_federal:
                _audit("gateway.adapter.blocked", name, "deny", reason="not_official")
                raise AdapterUnavailableError(f"adapter {name!r} is not in the federal allowlist")
            _audit("gateway.adapter.unverified", name, "allow", reason="not_official")
            _logger.warning(
                "registry: loading unofficial adapter %r (personal/enterprise only)", name
            )

        spec = specs.get(platform_name)
        if spec is None:
            _audit("gateway.adapter.skipped", name, "deny", reason="no_such_folder")
            msg = (
                f"adapter {name!r} is enabled but there is no "
                f"arcgateway/adapters/{platform_name}/ folder"
            )
            if is_federal:
                raise AdapterUnavailableError(msg)
            _logger.warning("registry: %s — skipping", msg)
            continue

        ctx = AdapterBuildContext(
            name=name,
            raw_config=block,
            on_message=on_message,
            default_agent_did=default_agent_did,
            tier=tier,
            require_pairing=require_pairing,
        )
        try:
            adapter = spec.build(ctx)
        except (AdapterUnavailableError, ImportError) as exc:
            _audit("gateway.adapter.skipped", name, "deny", reason=type(exc).__name__)
            if is_federal:
                raise AdapterUnavailableError(f"adapter {name!r} unavailable: {exc}") from exc
            _logger.warning(
                "registry: adapter %r unavailable — skipping (needs %s): %s",
                name,
                ", ".join(spec.requires) or "no extra packages",
                exc,
            )
            continue

        _audit(
            "gateway.adapter.loaded",
            name,
            "allow",
            reason="official" if platform_name in OFFICIAL_ADAPTERS else "unofficial",
        )
        if not isinstance(block.get("agent_did"), str) or not block.get("agent_did"):
            # No per-platform override: this adapter serves whatever
            # [gateway].agent_did happens to be at process startup — a live
            # rewrite of that shared default (e.g. an operator/deploy script
            # re-pointing a multi-agent fleet's config) followed by a
            # restart silently repoints this adapter at a different agent
            # with no in-band signal. Audited (not blocked — single-agent
            # deployments legitimately rely on the shared default) so the
            # risk is visible rather than silent (task 27).
            _audit(
                "gateway.adapter.shared_default_agent_did",
                name,
                "warn",
                reason="no_platform_agent_did_override",
            )
            _logger.warning(
                "registry: adapter %r has no [platforms.%s].agent_did override — "
                "serves [gateway].agent_did=%r, which changes on any config edit + "
                "restart with no in-band warning",
                name,
                name,
                default_agent_did,
            )
        _logger.info("registry: loaded adapter %r (agent_did=%s)", name, ctx.agent_did())
        adapters.append(adapter)

    return adapters


def _audit(action: str, name: str, outcome: str, *, reason: str) -> None:
    """Emit a registry audit event (swallowed by the audit layer per AU-5)."""
    emit_event(
        action=action,
        target=f"adapter:{name}",
        outcome=outcome,
        extra={"adapter": name, "reason": reason},
    )


__all__ = [
    "OFFICIAL_ADAPTERS",
    "AdapterBuildContext",
    "AdapterBuilder",
    "AdapterSpec",
    "AdapterUnavailableError",
    "build_adapters",
    "discover_adapters",
    "validate_adapter_name",
]
