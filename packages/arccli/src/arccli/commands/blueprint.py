"""`arc blueprint` — signed preset-config bootstrap (SPEC-047).

A blueprint is a versioned, optionally-signed TOML preset that bootstraps a deployment
in one command. Verbs:

  list                 packaged + ~/.arc/blueprints presets (name/version/tier/signed)
  show <name>          the resolved config overlay
  apply <name>         verify -> deep-merge UNDER the target's existing config -> write
  verify <name>        signature validity for the current tier
  sign <path>          operator-sign a user blueprint (writes the .arcsig sidecar)

**Materialize-to-disk (DC-8b):** ``apply`` writes the concrete ``arcagent.toml`` the
runtime flat-reads — it never adds a runtime merge layer. It deep-merges UNDER the
existing file (preserving identity + user keys); it is NOT a clobber-write like
``arc agent build``. The written tier is the stringency-max of deployment + blueprint,
so a blueprint can only raise a floor, never weaken federal.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

_logger = logging.getLogger("arccli.commands.blueprint")

_USER_BLUEPRINT_DIR = Path("~/.arc/blueprints").expanduser()


def _write(msg: str = "") -> None:
    sys.stdout.write(msg + "\n")


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    try:
        from arccli.formatting import print_table

        print_table(headers, rows)
    except ImportError:
        sys.stdout.write("  " + "  ".join(headers) + "\n")
        for row in rows:
            sys.stdout.write("  " + "  ".join(row) + "\n")


# ---------------------------------------------------------------------------
# Reusable apply core — driven directly by the AC-7 E2E with a capturing audit
# callback so the audit producers are proven on the REAL path.
# ---------------------------------------------------------------------------


def apply_to_disk(
    name: str,
    *,
    target: Path,
    deployment_tier: str,
    arc_dir: Path,
    user_dir: Path | None = None,
    audit: Callable[[str, dict[str, Any]], None] | None = None,
    dry_run: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Resolve + verify a blueprint, deep-merge it UNDER ``target``'s existing config,
    audit the apply (+ any granted relaxation), and materialize it to disk.

    Returns ``(target, merged_config)``. A single ``audit`` callback receives both
    ``tier.relaxation_granted`` (per relaxed knob) and ``blueprint.applied`` — routed to
    the operator WORM sink at enterprise/federal, else a structured log.
    """
    from arcagent.blueprints import apply_blueprint, dumps_toml, resolve_blueprint

    from arccli.commands.operator import operator_public_key

    blueprint = resolve_blueprint(
        name,
        tier=deployment_tier,
        user_dir=user_dir,
        operator_public_key=operator_public_key(arc_dir),
    )
    base = _read_existing(target)
    merged = apply_blueprint(blueprint, base, deployment_tier=deployment_tier)

    # A --dry-run must leave NO trace: it writes no config AND emits no WORM record.
    # Auditing an "applied" event for a run that applied nothing is false AU-9/10
    # provenance (SPEC-047 MED-2). Both live behind the same guard.
    if not dry_run:
        audit_apply(blueprint, merged, arc_dir, audit=audit)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dumps_toml(merged), encoding="utf-8")
    return target, merged


def audit_apply(
    blueprint: Any,
    merged: dict[str, Any],
    arc_dir: Path,
    *,
    audit: Callable[[str, dict[str, Any]], None] | None = None,
) -> None:
    """Audit a resolved apply: any granted relaxation (REQ-023) + ``blueprint.applied`` (REQ-015).

    Shared by ``arc blueprint apply`` and ``arc init --blueprint`` so both surfaces emit the
    same audit through the same (WORM-or-log) sink. Both events flow through one callback.
    """
    from arcagent.tiers import audit_tier_relaxations

    effective = str(merged.get("security", {}).get("tier", "personal"))
    sink = audit if audit is not None else _default_audit(arc_dir, effective)
    audit_tier_relaxations(merged.get("security", {}), effective, audit=sink)
    sink(
        "blueprint.applied",
        {
            "name": blueprint.name,
            "version": blueprint.version,
            "sha256": blueprint.sha256,
            "signer_did": blueprint.signer_did,
            "source": blueprint.source,
            "effective_tier": effective,
        },
    )


def _read_existing(target: Path) -> dict[str, Any]:
    """Parse ``target``'s current config so the merge preserves identity + user keys."""
    if not target.is_file():
        return {}
    try:
        return tomllib.loads(target.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        _logger.warning("existing %s is not valid TOML; treating as empty base", target)
        return {}


def _default_audit(arc_dir: Path, effective_tier: str) -> Callable[[str, dict[str, Any]], None]:
    """Route audit events to the operator WORM sink (enterprise/federal) or a log (personal)."""

    def _cb(event: str, details: dict[str, Any]) -> None:
        from arctrust import AuditEvent, emit

        record = AuditEvent(
            actor_did="operator",
            action=event,
            target=str(details.get("name") or details.get("knob") or "blueprint"),
            outcome="allow",
            tier=effective_tier,
            extra=details,
        )
        if effective_tier in ("enterprise", "federal"):
            try:
                sink = _worm_sink(arc_dir)
                emit(record, sink)
                return
            except Exception:  # reason: fail-open — audit setup must never block apply (AU-5)
                _logger.warning("blueprint WORM audit unavailable; logging the event instead")
        _logger.info("audit %s %s", event, record.model_dump_json())

    return _cb


def _worm_sink(arc_dir: Path) -> Any:
    from arctrust import WormSink

    from arccli.commands.operator import resolve_operator_signer

    chain = arc_dir / ".audit" / "blueprints.worm"
    chain.parent.mkdir(parents=True, exist_ok=True)
    return WormSink(chain, resolve_operator_signer(arc_dir))


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _list(args: argparse.Namespace) -> None:
    from arcagent.blueprints import list_blueprints

    from arccli.commands.operator import operator_public_key

    arc_dir = Path(getattr(args, "config_dir", None) or Path.home() / ".arc")
    rows = [
        [bp.name, bp.version, bp.tier, bp.source, _signed_label(bp)]
        for bp in list_blueprints(operator_public_key=operator_public_key(arc_dir))
    ]
    if rows:
        _print_table(["Name", "Version", "Tier", "Source", "Signed"], rows)
    else:
        _write("No blueprints found.")


def _signed_label(bp: Any) -> str:
    if bp.source == "packaged":
        return "provenance"
    return "signed" if bp.signed else "unsigned"


def _show(args: argparse.Namespace) -> None:
    from arcagent.blueprints import dumps_toml, resolve_blueprint

    from arccli.commands.operator import operator_public_key

    tier = getattr(args, "tier", None) or "personal"
    arc_dir = Path(getattr(args, "config_dir", None) or Path.home() / ".arc")
    bp = resolve_blueprint(args.name, tier=tier, operator_public_key=operator_public_key(arc_dir))
    _write(f"# blueprint: {bp.name} v{bp.version} (tier={bp.tier}, source={bp.source})")
    _write(dumps_toml(bp.overlay).rstrip())


def _verify(args: argparse.Namespace) -> None:
    from arcagent.blueprints import resolve_blueprint

    from arccli.commands.operator import operator_public_key

    tier = getattr(args, "tier", None) or "personal"
    arc_dir = Path(getattr(args, "config_dir", None) or Path.home() / ".arc")
    try:
        bp = resolve_blueprint(
            args.name, tier=tier, operator_public_key=operator_public_key(arc_dir)
        )
    except (FileNotFoundError, ValueError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        sys.exit(1)
    label = "provenance-trusted" if bp.source == "packaged" else (
        f"signed by {bp.signer_did}" if bp.signed else "UNSIGNED"
    )
    _write(f"{bp.name} v{bp.version}: {label} (tier={bp.tier}, sha256={bp.sha256[:16]})")


def _apply(args: argparse.Namespace) -> None:
    arc_dir = Path(getattr(args, "config_dir", None) or Path.home() / ".arc")
    agent_dir: str | None = getattr(args, "agent", None)
    target = (
        Path(agent_dir).expanduser().resolve() / "arcagent.toml"
        if agent_dir
        else arc_dir / "arcagent.toml"
    )
    deployment_tier = _deployment_tier(target, arc_dir)
    dry_run = getattr(args, "dry_run", False)

    try:
        _, merged = apply_to_disk(
            args.name,
            target=target,
            deployment_tier=deployment_tier,
            arc_dir=arc_dir,
            dry_run=dry_run,
        )
    except (FileNotFoundError, ValueError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        sys.exit(1)

    from arcagent.blueprints import dumps_toml

    if dry_run:
        _write(f"# --dry-run — merged config for {target} (not written):")
        _write(dumps_toml(merged).rstrip())
        return
    _write(f"Applied blueprint {args.name!r} -> {target}")
    _write(f"  effective tier: {merged.get('security', {}).get('tier')}")


def _deployment_tier(target: Path, arc_dir: Path) -> str:
    """The deployment's baseline tier: the target's own tier, else the ~/.arc default."""
    for path in (target, arc_dir / "arcagent.toml"):
        if path.is_file():
            try:
                raw = tomllib.loads(path.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError:
                continue
            tier = raw.get("security", {}).get("tier")
            if tier:
                return str(tier)
    return "personal"


def _sign(args: argparse.Namespace) -> None:
    """Operator-sign a user blueprint TOML (writes the .arcsig sidecar)."""
    path = Path(args.path).expanduser().resolve()
    if not path.is_file():
        sys.stderr.write(f"Error: file not found: {path}\n")
        sys.exit(1)

    from arcagent.capabilities.artifact_signing import write_signature

    from arccli.commands.operator import load_operator_key

    arc_dir = Path(getattr(args, "config_dir", None) or Path.home() / ".arc")
    operator = load_operator_key(arc_dir)
    # DC-4 known limitation: write_signature needs the raw seed; a vault_transit
    # (federal) operator key has no in-process seed and must sign out-of-band.
    seed = getattr(operator, "seed", None)
    if not seed:
        sys.stderr.write(
            "Error: the operator key has no in-process seed (vault_transit custody). "
            "`arc blueprint sign` needs an in-process operator/author key; a vault-held "
            "federal key must sign out-of-band.\n"
        )
        sys.exit(1)
    signer_did = f"operator:{operator.public_key.hex()[:16]}"
    sidecar = write_signature(path, path.read_bytes(), signer_did=signer_did, private_key=seed)
    _write(f"Signed {path.name} -> {sidecar.name}")


# ---------------------------------------------------------------------------
# Argparse dispatcher
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc blueprint",
        description="Signed preset-config bootstrap — list, show, apply, verify, sign.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    subs.add_parser("list", help="List packaged + user blueprints.")

    p = subs.add_parser("show", help="Print a blueprint's resolved config overlay.")
    p.add_argument("name")
    p.add_argument("--tier", default=None, help="Deployment tier for resolution.")
    p.add_argument("--dir", dest="config_dir", default=None, help="Config dir (default: ~/.arc).")

    p = subs.add_parser("apply", help="Verify + deep-merge a blueprint into a config file.")
    p.add_argument("name")
    p.add_argument("--agent", dest="agent", default=None, help="Per-agent dir target.")
    p.add_argument("--dir", dest="config_dir", default=None, help="Config dir (default: ~/.arc).")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="Print merged config.")

    p = subs.add_parser("verify", help="Report a blueprint's signature validity.")
    p.add_argument("name")
    p.add_argument("--tier", default=None, help="Deployment tier for the fail-closed gate.")
    p.add_argument("--dir", dest="config_dir", default=None, help="Config dir (default: ~/.arc).")

    p = subs.add_parser("sign", help="Operator-sign a user blueprint (.arcsig sidecar).")
    p.add_argument("path")
    p.add_argument("--dir", dest="config_dir", default=None, help="Config dir (default: ~/.arc).")

    return parser


_SUBCOMMAND_MAP = {
    "list": _list,
    "show": _show,
    "apply": _apply,
    "verify": _verify,
    "sign": _sign,
}


def blueprint_handler(args: list[str]) -> None:
    """Top-level handler for `arc blueprint <sub> [args]`."""
    parser = _build_parser()
    if not args:
        parser.print_help()
        sys.exit(0)
    parsed = parser.parse_args(args)
    if parsed.subcmd is None:
        parser.print_help()
        sys.exit(0)
    _SUBCOMMAND_MAP[parsed.subcmd](parsed)
