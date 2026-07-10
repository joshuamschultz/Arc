#!/usr/bin/env python3
"""Idempotent TOML overlay patches for the single-node deploy runbook.

`arc init` only generates the *baseline* arcagent.toml / gateway.toml
(docs/deploy/single-node.md §6-7). This script applies the additional
settings that runbook calls for — [eval] model, [modules.skills] adapter,
[platforms.web]/[platforms.telegram] on gateway.toml — using tomlkit so
re-running against an already-patched file is a no-op rather than a
duplicate block or a clobbered customization (e.g. a hand-set
allowed_user_ids is left alone unless --allowed-user-ids is passed).

Usage:
    deploy_node_overlays.py agent-config PATH [--provider anthropic] [--model claude-sonnet-5]
    deploy_node_overlays.py gateway-config PATH [--agent-did DID] [--enable-telegram]
                                                 [--allowed-user-ids ID [ID ...]]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import tomlkit


def _load(path: Path) -> tomlkit.TOMLDocument:
    if not path.exists():
        sys.stderr.write(f"error: {path} does not exist — run `arc init` first\n")
        sys.exit(1)
    return tomlkit.parse(path.read_text(encoding="utf-8"))


def _save(path: Path, doc: tomlkit.TOMLDocument) -> None:
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def apply_agent_overlay(path: Path, provider: str, model: str) -> None:
    """Set [eval] provider/model and turn on [modules.skills] (arcskill/personal).

    Safe on both ~/.arc/arcagent.toml and team/<agent>/arcagent.toml — same
    schema (arcagent/core/config.py::EvalConfig,
    arcagent/modules/skills/config.py::SkillsConfig).
    """
    doc = _load(path)

    eval_table = doc.setdefault("eval", tomlkit.table())
    eval_table["provider"] = provider
    eval_table["model"] = model

    modules = doc.setdefault("modules", tomlkit.table())
    skills = modules.setdefault("skills", tomlkit.table())
    skills["enabled"] = True
    skills_config = skills.setdefault("config", tomlkit.table())
    skills_config["adapter"] = "arcskill"
    skills_config["tier"] = "personal"

    _save(path, doc)
    print(f"  [+] {path}: [eval] {provider}/{model}, [modules.skills] arcskill")


def apply_gateway_overlay(
    path: Path,
    agent_did: str | None,
    enable_telegram: bool,
    allowed_user_ids: list[int] | None,
) -> None:
    """Enable the web chat adapter, wire agent_did, and optionally Telegram.

    Telegram's real field is `token_env` — `arc init`'s own generator writes
    the wrong key (`bot_token_env`); this always writes the correct one.
    """
    doc = _load(path)

    if agent_did:
        gateway = doc.setdefault("gateway", tomlkit.table())
        gateway["agent_did"] = agent_did

    platforms = doc.setdefault("platforms", tomlkit.table())
    web = platforms.setdefault("web", tomlkit.table())
    web["enabled"] = True

    if enable_telegram:
        telegram = platforms.setdefault("telegram", tomlkit.table())
        telegram["enabled"] = True
        telegram["token_env"] = "TELEGRAM_BOT_TOKEN"  # noqa: S105 — env var name, not a secret
        if "bot_token_env" in telegram:
            del telegram["bot_token_env"]  # drop the generator's wrong key if present
        if allowed_user_ids is not None:
            telegram["allowed_user_ids"] = allowed_user_ids
        elif "allowed_user_ids" not in telegram:
            telegram["allowed_user_ids"] = []  # fail-closed default

    _save(path, doc)
    telegram_note = ", [platforms.telegram] enabled" if enable_telegram else ""
    print(f"  [+] {path}: [platforms.web] enabled{telegram_note}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    agent_cmd = sub.add_parser("agent-config")
    agent_cmd.add_argument("path", type=Path)
    agent_cmd.add_argument("--provider", default="anthropic")
    agent_cmd.add_argument("--model", default="claude-sonnet-5")

    gw_cmd = sub.add_parser("gateway-config")
    gw_cmd.add_argument("path", type=Path)
    gw_cmd.add_argument("--agent-did", default=None)
    gw_cmd.add_argument("--enable-telegram", action="store_true")
    gw_cmd.add_argument("--allowed-user-ids", type=int, nargs="*", default=None)

    args = parser.parse_args()

    if args.command == "agent-config":
        apply_agent_overlay(args.path, args.provider, args.model)
    elif args.command == "gateway-config":
        apply_gateway_overlay(
            args.path, args.agent_did, args.enable_telegram, args.allowed_user_ids
        )


if __name__ == "__main__":
    main()
