"""SPEC-047 — the config-relaxable tier surface (one declared table + one helper).

Five different modules used to hand-roll the same "personal relaxable / federal floor
non-relaxable" idiom. This module declares, in one place:

* :data:`RELAXABLE_KNOBS` — every tier-relaxable knob, its federal floor, and whether
  personal/enterprise may relax it (REQ-020).
* :func:`resolve_tier_floor` — the one shared helper that enforces "explicit weaker value
  at a tier that forbids relaxation → fail closed", pins the floor when unset at federal,
  and audits a granted relaxation (REQ-021/022/023). The existing ``SecurityConfig``
  federal-crypto/breaker validators delegate to it (dedup, core NCLOC down).
* tier **stringency ordering** (federal > enterprise > personal) — the ``Tier`` StrEnum
  has predicates but no numeric order; blueprints need it for stringency-max tier merge.

Enforcement stays where it already lives (the ``SecurityConfig`` validator hook, the
dispatch budget resolver, the dynamic-loader import policy). This module owns the *policy
table + the shared decision*, never a new enforcement path (WIRE-don't-rebuild).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from arcagent.core.tier import Tier

_TIER_RANK: dict[str, int] = {Tier.PERSONAL: 0, Tier.ENTERPRISE: 1, Tier.FEDERAL: 2}


def tier_rank(tier: str) -> int:
    """Numeric stringency rank (personal=0 < enterprise=1 < federal=2)."""
    return _TIER_RANK[Tier(str(tier).lower())]


def stricter_tier(a: str, b: str) -> str:
    """Return the more stringent of two tiers (stringency-max)."""
    return a if tier_rank(a) >= tier_rank(b) else b


@dataclass(frozen=True)
class RelaxableKnob:
    """A tier-relaxable security knob and its federal floor.

    Attributes:
        name: The config field name (also the audit/error label).
        federal_floor: The value federal pins/forces. For ``smaller`` knobs this is the
            maximum allowed cap; ``None`` here means "must be set / cannot disable".
        relax_personal / relax_enterprise: May that tier loosen the knob below the floor?
        stricter_is: Ordering of "stronger" — ``exact`` (federal forces this exact value),
            ``smaller`` (a smaller number is stricter; ``None`` requested = disabled = weakest),
            or ``larger`` (a larger set/number is stricter).
        enforcement: Where the floor is actually enforced. ``"SecurityConfig"`` knobs are
            enforced by the delegating validator in ``core/config.py``; the others are
            declarative reference rows for ``arc ext verify`` + audit, enforced at their
            existing dispatch/loader sites (NOT relocated here).
    """

    name: str
    federal_floor: Any
    relax_personal: bool
    relax_enterprise: bool
    stricter_is: Literal["exact", "smaller", "larger"]
    enforcement: str = "SecurityConfig"


RELAXABLE_KNOBS: tuple[RelaxableKnob, ...] = (
    # --- enforced by the SecurityConfig model_validator (delegated) ---
    RelaxableKnob("require_fips", True, True, True, "exact"),
    RelaxableKnob("custody", "vault_transit", True, True, "exact"),
    RelaxableKnob("signing_algorithm", "ecdsa-p256", True, True, "exact"),
    RelaxableKnob("runaway_max_repeat", 8, True, True, "smaller"),
    RelaxableKnob("error_cascade_max", 5, True, True, "smaller"),
    # --- reference rows: relaxable knobs enforced at their existing sites ---
    RelaxableKnob("budget.max_tokens", 500_000, True, True, "smaller", enforcement="dispatch"),
    RelaxableKnob("budget.max_cost_usd", 10.0, True, True, "smaller", enforcement="dispatch"),
    RelaxableKnob("budget.max_requests", 500, True, True, "smaller", enforcement="dispatch"),
    RelaxableKnob("allow_all_imports", False, True, True, "exact", enforcement="dynamic_loader"),
)

SECURITY_CONFIG_KNOBS: tuple[RelaxableKnob, ...] = tuple(
    k for k in RELAXABLE_KNOBS if k.enforcement == "SecurityConfig"
)


def resolve_tier_floor(
    knob: RelaxableKnob,
    tier: str,
    requested: Any,
    *,
    was_set: bool,
    audit: Callable[[str, dict[str, Any]], None] | None = None,
) -> Any:
    """Return the value to use for ``knob`` at ``tier`` (or raise on a forbidden relaxation).

    Federal pins/forces the floor and rejects any explicit weaker value. Personal/enterprise
    may relax the knob when permitted, in which case a granted relaxation is audited.
    """
    tier_str = str(tier).lower()
    if tier_str == Tier.FEDERAL:
        return _resolve_federal(knob, requested, was_set=was_set)
    if not _is_weaker_than_floor(knob, requested):
        return requested
    if not _tier_may_relax(knob, tier_str):
        raise ValueError(
            f"{tier_str} tier may not relax {knob.name} below its floor "
            f"{knob.federal_floor!r} (fail-closed)"
        )
    if was_set and audit is not None:
        audit(
            "tier.relaxation_granted",
            {"knob": knob.name, "tier": tier_str, "requested": requested, "resolved": requested},
        )
    return requested


def _resolve_federal(knob: RelaxableKnob, requested: Any, *, was_set: bool) -> Any:
    """Federal floor enforcement — force exact, pin/reject smaller, honor stricter."""
    if not _is_weaker_than_floor(knob, requested):
        # Already at/above the floor (exact-match or a stricter smaller value) — honor it.
        return knob.federal_floor if knob.stricter_is == "exact" else requested
    if was_set:
        raise ValueError(
            f"federal tier requires {knob.name}={knob.federal_floor!r} "
            f"(SC-5/SC-13/IA-7) — refusing a looser/disabled value"
        )
    return knob.federal_floor


def _is_weaker_than_floor(knob: RelaxableKnob, requested: Any) -> bool:
    """True when ``requested`` is weaker (less stringent) than the federal floor."""
    if knob.stricter_is == "exact":
        return bool(requested != knob.federal_floor)
    if knob.stricter_is == "smaller":
        return requested is None or bool(requested > knob.federal_floor)
    return requested is None or bool(requested < knob.federal_floor)  # "larger"


def _tier_may_relax(knob: RelaxableKnob, tier: str) -> bool:
    if tier == Tier.PERSONAL:
        return knob.relax_personal
    if tier == Tier.ENTERPRISE:
        return knob.relax_enterprise
    return False


def audit_tier_relaxations(
    security: dict[str, Any],
    tier: str,
    *,
    audit: Callable[[str, dict[str, Any]], None],
) -> None:
    """Audit every granted relaxation in a resolved ``[security]`` block (REQ-023).

    Runs each explicitly-set SecurityConfig-enforced knob through
    :func:`resolve_tier_floor` so a personal/enterprise value looser than the federal
    floor emits ``tier.relaxation_granted``. Federal grants nothing (floors are forced),
    so this is a no-op there. This is the production producer for the relaxation audit on
    the blueprint-apply path — the Pydantic validator cannot do I/O, so the audit is
    driven here where an operator explicitly applies a preset.
    """
    if str(tier).lower() == Tier.FEDERAL:
        return
    for knob in SECURITY_CONFIG_KNOBS:
        if knob.name in security:
            resolve_tier_floor(knob, tier, security[knob.name], was_set=True, audit=audit)


__all__ = [
    "RELAXABLE_KNOBS",
    "SECURITY_CONFIG_KNOBS",
    "RelaxableKnob",
    "audit_tier_relaxations",
    "resolve_tier_floor",
    "stricter_tier",
    "tier_rank",
]
