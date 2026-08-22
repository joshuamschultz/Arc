"""Not everything an operator supplies is a credential, and a form must know which.

The Jira connect panel rendered all three of its fields as password dots:
``api_token``, ``email``, and ``base_url``. Two of those are configuration. A
masked ``https://yourcompany.atlassian.net`` buys nothing — there is no secret to
protect — and it costs the operator the one thing that form needed most, which is
the ability to see whether they typed it correctly. A typo there produces an
install that fails at probe with nothing to look at.

So ``[[secrets]]`` now says which of the values it asks for are actually secret:

* ``sensitive`` defaults to **true**. Forgetting to declare it masks the field.
  Opting out is the deliberate act, and it is a bundle author's call, made in the
  bundle — core has no list of "fields that are probably URLs".
* A **non-sensitive** value may be read back out of the store and shown. This is a
  narrowing of D-583 ("a key value is write-only"), and it is deliberate: that
  rule exists to stop a *credential* reaching a surface, and a base URL is not a
  credential. Showing an operator the URL they configured, instead of making them
  retype it blind on every rotation, is the whole point.
* A **sensitive** value keeps exactly the treatment it has today: no route out of
  the store, no response field able to hold it, no log line, no audit event.

The mechanism is what these tests are strictest about. The read-back is driven by
the manifest's own flag and by nothing else — there is no argument a caller can
pass to widen it, so no route, no bug, and no future caller can turn it into
"show me everything". ``test_reading_back_takes_no_argument_that_could_widen_it``
asserts that on the signature itself.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from arcstore.backends.memory import FakeBackend

from arcagent.connections import Connections
from arcagent.core.tier import Tier
from arcagent.extension.manifest import load_manifest

_SENSITIVE = "zzz-sensitive-value-5501"
_PUBLIC = "https://yourcompany.example.net"

_EXTENSION = "acme_fields"
_INSTANCE = "work"

_MANIFEST = f"""
[extension]
name = "{_EXTENSION}"
version = "1.0.0"
attachment = "native"
description = "Acme, configured with a URL and authorised with a token."

[config.native]
entrypoint = "acme_fields_attachment"

[[secrets]]
name = "api_token"
prompt = "Acme API token, from the Acme console under Settings then API tokens."

[[secrets]]
name = "base_url"
prompt = "Your Acme web address, exactly as it appears in the browser bar."
sensitive = false

[tools]
allow = ["ping"]

[[tools.declared]]
name = "ping"
description = "Report the Acme client version."
classification = "read_only"

[approval]
default = "outbound"
"""

_ADAPTER = '''
"""The acme fixture's own implementation, outside every Arc package."""

from __future__ import annotations

from typing import Any

from arcagent.extension.attachment import ProbeResult, ToolResult, ToolSpec


class AcmeFieldsAttachment:
    def __init__(self, context: dict[str, Any]) -> None:
        self._token = str(context.get("api_token") or "")

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        if not self._token:
            return ProbeResult(reachable=False, detail="acme has no credential for api_token")
        return ProbeResult(reachable=True, tools=await self.describe_tools(), detail="ok")

    async def describe_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="ping", description="Report the Acme client version.")]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, content="acme 1.0.0")


def build_native_attachment(context: dict[str, Any]) -> AcmeFieldsAttachment:
    return AcmeFieldsAttachment(context)
'''


# --- what the manifest says ---------------------------------------------------


def _parsed(text: str = _MANIFEST) -> list[tuple[str, bool]]:
    manifest = load_manifest(text, tier=Tier.PERSONAL)
    return [(declared.name, declared.sensitive) for declared in manifest.secrets]


def test_a_bundle_says_which_of_its_fields_are_really_secret() -> None:
    """One flag, declared where the field is declared."""
    assert _parsed() == [("api_token", True), ("base_url", False)]


def test_a_field_that_says_nothing_is_treated_as_a_credential() -> None:
    """Fail-safe. Forgetting the flag must mask, never expose.

    Written against a manifest with the clause REMOVED rather than by reading a
    model default, because the default is what a future edit would change and
    this is the assertion that would catch it.
    """
    assert _parsed(_MANIFEST.replace("sensitive = false\n", "")) == [
        ("api_token", True),
        ("base_url", True),
    ]


# --- what comes back out ------------------------------------------------------


@pytest.fixture
def connected(tmp_path: Path) -> Connections:
    """One deployment with the acme bundle available and both fields supplied."""
    arc_dir = tmp_path / "arc"
    bundle = arc_dir / "extensions" / _EXTENSION
    bundle.mkdir(parents=True)
    (bundle / "extension.toml").write_text(_MANIFEST, encoding="utf-8")
    (bundle / "acme_fields_attachment.py").write_text(_ADAPTER, encoding="utf-8")

    backend = FakeBackend()

    async def open_backend() -> FakeBackend:
        return backend

    return Connections.for_deployment(
        arc_dir=arc_dir,
        data_dir=tmp_path / "data",
        state_opener=open_backend,
    )


async def _install(connections: Connections) -> None:
    plan = connections.plan(_EXTENSION, _INSTANCE)
    await connections.install(plan, {"api_token": _SENSITIVE, "base_url": _PUBLIC})


async def test_a_non_sensitive_value_is_shown_back_to_the_operator(
    connected: Connections,
) -> None:
    """The narrowing of D-583, stated as behaviour: the URL comes back.

    Without this an operator rotating a token has to retype a base URL they
    cannot see and never changed, and a single typo in it fails the probe.
    """
    await _install(connected)

    auth = await connected.authorization(_INSTANCE)
    supplied = {field.name: field for field in auth.credentials}

    assert supplied["base_url"].value == _PUBLIC
    assert supplied["base_url"].sensitive is False


async def test_the_credential_beside_it_has_no_route_out_of_the_store(
    connected: Connections,
) -> None:
    """The assertion that matters: present and absent in the SAME answer.

    A test that only checked an all-sensitive connection would pass against an
    implementation that read every value and forgot to filter, because there
    would be nothing to compare against. Here the URL proves the read-back path
    ran, and the token proves it did not run for a credential.
    """
    await _install(connected)

    auth = await connected.authorization(_INSTANCE)
    supplied = {field.name: field for field in auth.credentials}

    assert supplied["api_token"].sensitive is True
    assert supplied["api_token"].value == ""
    # No field anywhere in the answer holds it, not merely the one checked above.
    assert _SENSITIVE not in repr(auth)


async def test_every_declared_field_is_still_offered_even_with_nothing_stored(
    connected: Connections,
) -> None:
    """A first-time connection must still draw a complete form.

    Read-back answers with an empty value rather than dropping the field, because
    a form built from this list would otherwise lose the input an operator needs
    most on the connection that has never been configured.
    """
    await _install(connected)
    (connected.world.env_file).write_text("", encoding="utf-8")
    (connected.world.env_file).chmod(0o600)

    auth = await connected.authorization(_INSTANCE)

    assert [field.name for field in auth.credentials] == ["api_token", "base_url"]
    assert all(field.value == "" for field in auth.credentials)


def test_reading_back_takes_no_argument_that_could_widen_it() -> None:
    """No ``include_secrets=True`` may ever exist, so no caller can ask for one.

    The flag is the only input to the decision. A boolean parameter here would be
    one call site away from a surface that renders every value, and that call site
    would look reasonable in review — which is why the constraint is asserted on
    the signature rather than trusted to convention.
    """
    signature = inspect.signature(Connections.authorization)

    assert list(signature.parameters) == ["self", "instance"]
