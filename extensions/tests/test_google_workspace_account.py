"""Each Google connection binds one account, and the standard tool set stays fixed.

A connection carries which account it is through its ``account`` field. The native
attachment checks the signed-in address against it, so two connections of the same
bundle never read each other's mailbox, and no tool can be told another account,
client or home. Tool behaviour is covered in ``test_google_native_tools``; this file
holds the account-routing contract the manifest and the attachment share.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
from arcagent.core.tier import Tier
from arcagent.extension.attachment import ProbeResult
from arcagent.extension.manifest import load_manifest

from extensions.google_workspace.arc_ext_google_workspace.native import build_native_attachment
from extensions.tests.fake_credential import FakeCredentialHandle

BUNDLE = Path(__file__).resolve().parents[1] / "google_workspace"

_SENDS = {
    "google_gmail_send",
    "google_gmail_draft_send",
    "google_gmail_reply",
    "google_gmail_reply_all",
    "google_gmail_forward",
}
_FORBIDDEN_ARGUMENTS = {"account", "client", "home", "access_token", "token", "attach"}


def _manifest():  # type: ignore[no-untyped-def]
    return load_manifest(
        (BUNDLE / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL
    )


def _attachment(account: str, signed_in_as: str) -> Any:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"emailAddress": signed_in_as})

    return build_native_attachment(
        {
            "credential": FakeCredentialHandle(["token"]),
            "account": account,
            "read_only": "yes",
            "transport": httpx.MockTransport(answer),
        }
    )


def _probe(account: str, signed_in_as: str) -> ProbeResult:
    return asyncio.run(_attachment(account, signed_in_as).probe())


def _specs() -> dict[str, Any]:
    return {spec.name: spec for spec in asyncio.run(_attachment("", "").describe_tools())}


def test_every_google_call_is_routed_and_read_only_is_a_switch() -> None:
    manifest = _manifest()
    routing = manifest.tools.routing
    assert routing is not None
    assert (routing.argument, routing.field) == ("account", "account")
    assert manifest.tools.read_only is not None


def test_each_connection_reads_only_its_own_account() -> None:
    assert _probe("josh@blackarcindustrial.com", "josh@blackarcindustrial.com").reachable
    assert _probe("josh@blackarcsystems.com", "josh@blackarcsystems.com").reachable
    crossed = _probe("josh@blackarcindustrial.com", "josh@blackarcsystems.com")
    assert not crossed.reachable
    assert crossed.detail == "signed in as a different account"


def test_the_account_comparison_ignores_case_but_not_look_alikes() -> None:
    assert _probe("Josh@BlackArcIndustrial.com", "josh@blackarcindustrial.com").reachable
    assert not _probe("josh@blackarcindustrial.com", "josh@blackarcindustrial.co").reachable


def test_a_connection_with_no_bound_account_accepts_the_signed_in_one() -> None:
    assert _probe("", "anyone@example.com").reachable


def test_no_tool_can_be_told_another_account_client_or_home() -> None:
    for spec in _specs().values():
        arguments = set(spec.input_schema["properties"])
        assert not arguments & _FORBIDDEN_ARGUMENTS, spec.name


def test_the_native_tool_set_is_the_declared_one() -> None:
    declared = {tool.name: tool for tool in _manifest().tools.declared}
    specs = _specs()
    assert set(specs) == set(declared)
    for name, spec in specs.items():
        assert spec.classification == declared[name].classification, name


def test_only_the_send_verbs_are_egress() -> None:
    declared = {tool.name: tool for tool in _manifest().tools.declared}
    for name, tool in declared.items():
        assert ("network_egress" in tool.capability_tags) == (name in _SENDS), name
    for name, spec in _specs().items():
        assert ("network_egress" in spec.capability_tags) == (name in _SENDS), name


def test_every_page_size_is_bounded() -> None:
    for name, spec in _specs().items():
        for argument in ("limit", "max"):
            prop = spec.input_schema["properties"].get(argument)
            if prop is not None:
                assert prop.get("maximum"), f"{name}.{argument}"
