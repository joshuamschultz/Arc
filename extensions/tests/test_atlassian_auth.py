"""What Atlassian actually receives, and what an operator is told when it refuses.

Confluence only, and that is the point of the file now. Jira used to be here, and
the failure below is Jira's; the answer to it was not a better basic-auth header
but Atlassian's own client, so that bundle wraps ``acli`` and its credential
never crosses a socket Arc opened. Confluence still owns its transport — ``acli``
1.3.22 offers ``confluence page view`` and nothing that searches, creates or
updates a page — so every property below still has to hold for it.

The live failure this exists for. An operator connected Jira through arcui with a
real token, a real email and the correct address, and got:

    probe: jira did not answer — https://ctgfederal.atlassian.net did not answer:
    Client error '401 Unauthorized' for url
    'https://ctgfederal.atlassian.net/rest/api/3/myself'
    For more information check: https://developer.mozilla.org/…/Status/401

Two things were missing, and neither was visible from the code.

**Nothing proved the credential arrives intact.** ``auth=(email, token)`` reads
correct, but no test had ever decoded the header a shipped bundle puts on the
wire. A token pasted from Atlassian's dialog can carry a trailing newline or a
space the operator cannot see, and a value with no declared ``format`` was stored
byte for byte — so the invisible character travelled all the way to Atlassian and
came back as a 401 nobody could explain. So a real socket server sits here,
decodes the ``Authorization`` header, and asserts it carries exactly the email and
token that were supplied — including on the install path, which is the wiring that
would have to apply the shape and could silently stop doing so.

**A 401 was reported in httpx's words.** An MDN page about HTTP status codes tells
the person who has just pasted a token nothing about what to do. 401, 403 and 404
mean three different things on this API and need three different actions, so each
is asserted on the sentence an operator reads.
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from arcagent.core.tier import Tier
from arcagent.extension.attachment import ExtensionAttachment
from arcagent.extension.grants import ConnectionRegistry
from arcagent.extension.manifest import ExtensionManifest, load_manifest
from arcagent.extension.secrets import (
    LocalFileSecretBackend,
    Secret,
    SecretRef,
    SecretStore,
)
from arcagent.extension.state import open_connection_state
from arcagent.modules.connectors.install import (
    AttachmentFactory,
    ConnectorPlan,
    build_attachment,
    install_connector,
)

EXTENSIONS_ROOT = Path(__file__).resolve().parents[1]

#: Recorded as the actor on the install path's credential operations.
_CALLER = "did:arc:testorg:executor/atlassian"

#: The credential pair under test. The token is a sentinel: no refusal, log line
#: or probe detail anywhere below may contain it.
_EMAIL = "operator@ctgfederal.example"
_TOKEN = "ATATT3xFfGF0-sentinel-Rk9SQklEREVO-000"

#: What each Atlassian bundle's probe asks for, and the body a live site answers
#: it with. Keyed by bundle name so a third Atlassian bundle joins by adding a row.
#:
#: ``jira`` is deliberately absent and its absence is the fix, not a gap: that
#: bundle no longer speaks this API. It runs ``acli``, which owns its own
#: authentication, so there is no basic-auth header of ours to decode — its
#: equivalent assertions are in ``test_connector_bundles`` (the argv each verb
#: becomes, and its sign-in check replayed against both real states) and in
#: ``test_host_login`` (the token reaching stdin, shaped, and never argv).
_PROBE_PATHS: dict[str, tuple[str, dict[str, Any]]] = {
    "confluence": ("/wiki/rest/api/space", {"results": []}),
}

BUNDLE_IDS = sorted(_PROBE_PATHS)


# --- a stand-in Atlassian ------------------------------------------------------


@dataclass
class _Site:
    """An Atlassian that verifies the credential it is given, and records it.

    ``answer`` is the status it replies with regardless — how a test asks for the
    401, 403 and 404 an operator has to be able to act on.
    """

    answer: int = 200
    seen: list[str] = field(default_factory=list)

    @property
    def credential(self) -> tuple[str, str] | None:
        """The email and token decoded from the last request's basic auth."""
        return _decode_basic(self.seen[-1]) if self.seen else None


def _decode_basic(header: str) -> tuple[str, str] | None:
    """The two halves of an HTTP basic credential, or None if it is not one."""
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    user, separator, password = decoded.partition(":")
    return (user, password) if separator else None


def _handler(site: _Site) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            site.seen.append(self.headers.get("Authorization", ""))
            if site.answer != 200:
                self._reply(site.answer, {"errorMessages": ["refused"]})
                return
            if site.credential != (_EMAIL, _TOKEN):
                self._reply(401, {"errorMessages": ["Basic auth with the wrong credential"]})
                return
            self._reply(200, _body_for(self.path))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — stdlib's name
            """Silence stdlib's stderr access log; pytest owns this output."""

        def _reply(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def _body_for(path: str) -> dict[str, Any]:
    for probe_path, body in _PROBE_PATHS.values():
        if path.startswith(probe_path):
            return body
    return {}


@contextmanager
def _serving(site: _Site) -> Iterator[str]:
    """Run the stand-in on a loopback port and yield its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(site))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --- the shipped bundles, built the way the install path builds them ------------


def _manifest(bundle: str) -> ExtensionManifest:
    path = EXTENSIONS_ROOT / bundle / "extension.toml"
    return load_manifest(path.read_text(encoding="utf-8"), tier=Tier.PERSONAL)


def _attachment(bundle: str, *, base_url: str, email: str, token: str) -> ExtensionAttachment:
    """The real attachment, through the real factory, holding real Secrets."""
    return build_attachment(
        _manifest(bundle),
        EXTENSIONS_ROOT / bundle,
        {
            "base_url": Secret(base_url),
            "email": Secret(email),
            "api_token": Secret(token),
        },
    )


# --- the credential reaches the service intact ----------------------------------


@pytest.mark.parametrize("bundle", BUNDLE_IDS)
async def test_the_service_receives_exactly_the_credential_that_was_supplied(
    bundle: str,
) -> None:
    """Basic auth, carrying the email and the token, byte for byte.

    The assertion nothing made before: not that the adapter *builds* auth, but
    that the bytes on the wire decode back to the pair the operator typed.
    """
    site = _Site()
    with _serving(site) as base_url:
        result = await _attachment(bundle, base_url=base_url, email=_EMAIL, token=_TOKEN).probe()

    assert result.reachable, result.detail
    assert site.credential == (_EMAIL, _TOKEN)


@pytest.mark.parametrize("bundle", BUNDLE_IDS)
async def test_a_probe_cannot_pass_on_a_credential_the_service_did_not_verify(
    bundle: str,
) -> None:
    """The property that makes a green probe mean anything: identity, not reach.

    Several of this API's read endpoints answer 200 to an anonymous caller, so a
    probe pointed at one would report a connection as working while the token it
    was given authenticated nothing — indistinguishable, to the caller, from an
    account that is genuinely connected and can simply see very little. Both
    bundles probe an endpoint that refuses an unverified caller, and this is what
    holds them there: swap either probe for a route that answers anonymously and
    the wrong credential below stops being refused.
    """
    site = _Site()
    with _serving(site) as base_url:
        result = await _attachment(
            bundle, base_url=base_url, email=_EMAIL, token="not-the-stored-token"
        ).probe()

    assert not result.reachable, "a probe that passes without a verified credential proves nothing"
    assert site.credential == (_EMAIL, "not-the-stored-token"), "the wrong token was truly sent"


def _redirected(base_url: str) -> AttachmentFactory:
    """The shipped factory, with only the ADDRESS pointed at the stand-in.

    Core refuses to store a plaintext address, and it is right to: a credential
    goes over this connection on every call. So the address is the one value a
    loopback test cannot put through the store, and it is the only one substituted
    here. The credential still travels the whole real path — shaped on the way in,
    written to the store, read back out of it, revealed into the bundle's own
    adapter, and put on the wire — which is the path under test.
    """

    def build(
        manifest: ExtensionManifest, bundle: Path, secrets: Mapping[str, Secret]
    ) -> ExtensionAttachment:
        return build_attachment(manifest, bundle, {**secrets, "base_url": Secret(base_url)})

    return build


@pytest.mark.parametrize("bundle", BUNDLE_IDS)
async def test_a_credential_pasted_with_invisible_whitespace_still_authenticates(
    bundle: str, tmp_path: Path
) -> None:
    """The reported shape, driven through the WHOLE install, not through the helper.

    A token copied out of Atlassian's dialog arrives with a trailing newline, and an
    email typed into a form arrives with the space that came with the paste. Both
    are invisible, and both used to travel to Atlassian unchanged and come back as a
    401 the operator could do nothing with.

    This runs ``install_connector`` — resolve, verify, secrets, probe, persist — so
    it fails if the shaping is applied anywhere other than the wiring every surface
    goes through. Take :func:`shape_supplied` out of ``_write_secrets`` and this
    test is what catches it: what the store holds is asserted as well as what the
    service receives, because those are the two things a later agent run depends on.
    """
    site = _Site()
    manifest = _manifest(bundle)
    store = SecretStore(LocalFileSecretBackend(tmp_path / "arc.env"))
    with _serving(site) as base_url:
        report = await install_connector(
            ConnectorPlan(
                instance="primary",
                extension=manifest.extension.name,
                bundle=EXTENSIONS_ROOT / bundle,
                manifest=manifest,
                unsatisfied_host=(),
                secrets=tuple(manifest.secrets),
                approval_mode=manifest.approval.default,
                tier=Tier.PERSONAL,
                extensions_root=(EXTENSIONS_ROOT,),
            ),
            connections=ConnectionRegistry(tmp_path),
            agents=("bundle_agent",),
            secret_values={
                "base_url": "https://ctgfederal.example",
                "email": f" {_EMAIL} ",
                "api_token": f"{_TOKEN}\n",
            },
            store=store,
            caller_did=_CALLER,
            state=await open_connection_state(str(tmp_path / "data")),
            attachment_factory=_redirected(base_url),
        )

    assert report.tools, "a completed install serves the bundle's verbs"
    assert site.credential == (_EMAIL, _TOKEN), (
        "the service must receive the clean credential, not what the paste carried"
    )
    ref = SecretRef(connection="primary", field="api_token")
    stored = await store.get(ref, caller_did=_CALLER)
    assert stored is not None and stored.reveal() == _TOKEN, (
        "every later agent run resolves the credential out of the store, so the "
        "clean value has to be what is stored — not something cleaned at probe time"
    )


# --- a refusal an operator can act on -------------------------------------------


async def _detail(bundle: str, answer: int) -> str:
    site = _Site(answer=answer)
    with _serving(site) as base_url:
        result = await _attachment(bundle, base_url=base_url, email=_EMAIL, token=_TOKEN).probe()
    assert not result.reachable
    return result.detail


@pytest.mark.parametrize("bundle", BUNDLE_IDS)
async def test_a_401_names_the_two_fields_that_have_to_match(bundle: str) -> None:
    """The reported failure. 401 here means the email and token are not one account.

    Naming both fields is the whole point: an operator shown "401 Unauthorized"
    has no way to know that the token might be perfectly valid and simply belong
    to a different Atlassian login.
    """
    detail = await _detail(bundle, 401)

    assert "email" in detail.lower()
    assert "token" in detail.lower()
    assert "id.atlassian.com" in detail, "say where to reissue it"


@pytest.mark.parametrize("bundle", BUNDLE_IDS)
async def test_a_403_is_about_permission_and_not_about_the_credential(bundle: str) -> None:
    """403 is authenticated-but-not-permitted. Sending them to reissue a working
    token is the wrong instruction, so the two must not read alike."""
    detail = await _detail(bundle, 403)

    assert "id.atlassian.com" not in detail, "the credential is fine; reissuing it fixes nothing"
    assert "administrator" in detail.lower()


@pytest.mark.parametrize("bundle", BUNDLE_IDS)
async def test_a_404_points_at_the_address_and_not_at_the_credential(bundle: str) -> None:
    """404 means the site is wrong, which is a different field entirely."""
    detail = await _detail(bundle, 404)

    assert "address" in detail.lower()
    assert "id.atlassian.com" not in detail


@pytest.mark.parametrize("bundle", BUNDLE_IDS)
@pytest.mark.parametrize("answer", [401, 403, 404, 500])
async def test_no_refusal_shows_httpxs_words_or_the_token(bundle: str, answer: int) -> None:
    """An MDN link is what httpx says; it is never what an operator needs.

    And the sentinel: a refusal is rendered in a browser, returned on an HTTP
    response, and written to a log, so the credential must not be in it.
    """
    detail = await _detail(bundle, answer)

    assert _TOKEN not in detail
    assert "developer.mozilla.org" not in detail
    assert "Client error" not in detail
