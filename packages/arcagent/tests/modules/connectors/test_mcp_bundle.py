"""P12 (RED first) — an operator-added MCP server becomes a signed connector bundle.

An MCP server is third-party code reached over a wire or a child process, so the
generated bundle is the whole trust boundary: what it may launch, where it may
connect, which tools it may expose, and who vouched for those bytes. These tests
pin each refusal where the spec is built, before anything is written or signed.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest
from arctrust.audit import NullSink
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey
from pydantic import ValidationError

from arcagent.capabilities import artifact_signing
from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.loader import ExtensionLoader
from arcagent.extension.manifest import load_manifest
from arcagent.modules.connectors.mcp_bundle import (
    McpServerSpec,
    McpToolChoice,
    render_extension_toml,
    sign_bundle,
    spec_digest,
    validate_spec,
    write_bundle,
)

_PYTHON = sys.executable
_OPERATOR_DID = "did:arc:operator:test"


def _public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


def _http_spec(**overrides: object) -> McpServerSpec:
    fields: dict[str, object] = {
        "name": "acme",
        "display": "Acme Tracker",
        "description": "Search and update Acme tickets.",
        "transport": "http",
        "url": "https://mcp.acme.example/v1/mcp",
        "auth_header": "Authorization",
        "auth_scheme": "Bearer",
        "tools": {
            "search_tickets": McpToolChoice(classification="read_only", capability_tags=["web"]),
            "close-ticket": McpToolChoice(capability_tags=["network_egress"]),
        },
    }
    fields.update(overrides)
    return McpServerSpec(**fields)  # type: ignore[arg-type]


def _stdio_spec(argv: list[str] | None = None, **overrides: object) -> McpServerSpec:
    fields: dict[str, object] = {
        "name": "localfs",
        "display": "Local files",
        "description": "Read files from a folder.",
        "transport": "stdio",
        "argv": argv if argv is not None else [_PYTHON, "-m", "some_server"],
        "tools": {"read_file": McpToolChoice(classification="read_only")},
    }
    fields.update(overrides)
    return McpServerSpec(**fields)  # type: ignore[arg-type]


def _validated(spec: McpServerSpec, tier: Tier = Tier.PERSONAL, **kwargs: object) -> McpServerSpec:
    kwargs.setdefault("resolver", _public_resolver)
    return validate_spec(spec, tier=tier, **kwargs)  # type: ignore[arg-type]


def _refusal(spec: McpServerSpec, tier: Tier = Tier.PERSONAL, **kwargs: object) -> str:
    with pytest.raises(ExtensionError) as caught:
        _validated(spec, tier, **kwargs)
    return caught.value.message


# --- shape --------------------------------------------------------------------


def test_spec_is_frozen_and_refuses_unknown_keys() -> None:
    spec = _http_spec()
    with pytest.raises(ValidationError):
        spec.name = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        McpServerSpec(  # type: ignore[call-arg]
            name="acme", transport="http", url="https://x.example/", surprise=True
        )


@pytest.mark.parametrize("name", ["Acme", "1acme", "ac-me", "a" * 33, "", "../x", "acme/x"])
def test_spec_name_must_be_a_coordinate(name: str) -> None:
    with pytest.raises(ValidationError):
        _http_spec(name=name)


def test_transport_fields_must_not_mix() -> None:
    with pytest.raises(ValidationError):
        _http_spec(argv=["/bin/true"])
    with pytest.raises(ValidationError):
        _stdio_spec(url="https://x.example/")
    with pytest.raises(ValidationError):
        _http_spec(url="")
    with pytest.raises(ValidationError):
        _stdio_spec(argv=[])


# --- rendering ----------------------------------------------------------------


def test_render_matches_composio_shape() -> None:
    spec = _validated(_http_spec())
    manifest = load_manifest(render_extension_toml(spec), tier=Tier.PERSONAL)

    assert manifest.extension.attachment == "mcp"
    assert manifest.extension.name == "acme"
    assert manifest.health is not None and manifest.health.probe == "attachment"
    assert manifest.oauth is None
    assert manifest.knowledge.mode == "non_indexable"
    assert "tool" in manifest.knowledge.reason.lower()
    declared = {tool.name: tool for tool in manifest.tools.declared}
    assert manifest.tools.allow is not None
    assert sorted(manifest.tools.allow) == sorted(declared)
    assert set(declared) == {"acme__search_tickets", "acme__close-ticket"}
    assert declared["acme__search_tickets"].classification == "read_only"
    assert declared["acme__close-ticket"].classification == "state_modifying"
    mcp = manifest.config["mcp"]
    assert mcp["transport"] == "http"
    assert mcp["namespace"] == "acme"
    assert mcp["url_origin"] == "https://mcp.acme.example/"
    assert mcp["tools"]["search_tickets"]["classification"] == "read_only"
    assert manifest.approval.default == "outbound"


def test_render_never_carries_a_credential_value() -> None:
    spec = _validated(_http_spec())
    text = render_extension_toml(spec)
    assert "sensitive = true" in text
    assert "Bearer" in text  # the scheme is configuration, the token is not


def test_render_stdio_places_credentials_by_environment_name() -> None:
    spec = _validated(_stdio_spec(env_refs={"api_key": "ACME_API_KEY"}))
    manifest = load_manifest(render_extension_toml(spec), tier=Tier.PERSONAL)
    secret = next(item for item in manifest.secrets if item.name == "api_key")
    assert secret.placement is not None and secret.placement.variable == "ACME_API_KEY"
    assert secret.sensitive is True
    assert manifest.config["mcp"]["argv"][0] == spec.argv[0]


def test_render_requires_at_least_one_chosen_tool() -> None:
    with pytest.raises(ExtensionError):
        render_extension_toml(_http_spec(tools={}))


# --- http: https, origin, SSRF -----------------------------------------------


def test_https_only_and_origin_pinned() -> None:
    assert "https" in _refusal(_http_spec(url="http://mcp.acme.example/mcp"))
    assert "userinfo" in _refusal(_http_spec(url="https://user:pw@mcp.acme.example/mcp"))
    assert "query" in _refusal(_http_spec(url="https://mcp.acme.example/mcp?api_key=abc"))
    pinned = _validated(_http_spec(url="https://mcp.acme.example:8443/mcp"))
    manifest = load_manifest(render_extension_toml(pinned), tier=Tier.PERSONAL)
    assert manifest.config["mcp"]["url_origin"] == "https://mcp.acme.example:8443/"


def test_loopback_http_is_a_personal_tier_convenience_only() -> None:
    local = _http_spec(url="http://127.0.0.1:8931/mcp")
    assert _validated(local).url == "http://127.0.0.1:8931/mcp"
    assert "https" in _refusal(local, Tier.ENTERPRISE, stdio_allow=("x",))


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data",
        "https://[fe80::1]/mcp",
        "https://[::ffff:169.254.169.254]/mcp",
        "https://metadata.google.internal/mcp",
        "https://0.0.0.0/mcp",
        "https://224.0.0.1/mcp",
        "https://2852039166/mcp",
        "https://0xa9fea9fe/mcp",
        "http://169.254.169.254/mcp",
        "http://localhost.evil.example.169.254.nip.io/mcp",
    ],
)
def test_ssrf_guard_refuses_link_local_and_metadata_hosts(url: str) -> None:
    def resolver(host: str) -> list[str]:
        return ["169.254.169.254"] if host.endswith("nip.io") else [host]

    with pytest.raises(ExtensionError):
        _validated(_http_spec(url=url), resolver=resolver)


@pytest.mark.parametrize(
    "host", ["2852039166", "0xa9fea9fe", "0251.0376.0251.0376", "169.254.43518"]
)
def test_a_numeric_host_is_refused_even_if_the_resolver_calls_it_public(host: str) -> None:
    """Whatever a resolver makes of a numeric trick, the spec never reaches it."""
    message = _refusal(_http_spec(url=f"https://{host}/mcp"), resolver=_public_resolver)
    assert "numeric" in message


def test_a_plain_http_remote_is_refused_before_any_dns_lookup() -> None:
    def resolver(host: str) -> list[str]:
        raise AssertionError("a refused scheme must not cause a network lookup")

    assert "https" in _refusal(_http_spec(url="http://mcp.acme.example/mcp"), resolver=resolver)


def test_ssrf_guard_refuses_a_name_that_resolves_to_a_metadata_address() -> None:
    def resolver(host: str) -> list[str]:
        return ["93.184.216.34", "169.254.169.254"]

    message = _refusal(_http_spec(), resolver=resolver)
    assert "169.254.169.254" in message


def test_an_unresolvable_host_is_refused_not_guessed() -> None:
    def resolver(host: str) -> list[str]:
        raise OSError("no such host")

    assert "resolve" in _refusal(_http_spec(), resolver=resolver)


def test_header_name_must_be_a_plain_header() -> None:
    for header in ("Host", "Content-Length", "X Bad", "x:y", "x\r\ninjected"):
        with pytest.raises(ExtensionError):
            _validated(_http_spec(auth_header=header))


# --- stdio: allowed executable only -------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [_PYTHON, "-m", "x; rm -rf /"],
        [_PYTHON, "-m", "x && curl evil"],
        [_PYTHON, "-m", "x | tee"],
        [_PYTHON, "-m", "$(whoami)"],
        [_PYTHON, "-m", "`id`"],
        [_PYTHON, "-m", "a>b"],
        [_PYTHON, "-m", "line\nbreak"],
        [_PYTHON + ";id"],
    ],
)
def test_argv_shell_metachar_refused(argv: list[str]) -> None:
    assert "metacharacter" in _refusal(_stdio_spec(argv=argv))


@pytest.mark.parametrize("command", ["./server", "../bin/server", "bin/server", "~/server"])
def test_relative_command_paths_are_refused(command: str) -> None:
    assert "absolute" in _refusal(_stdio_spec(argv=[command]))


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "env", "sudo"])
def test_shells_and_wrappers_are_not_servers(shell: str) -> None:
    assert "interpreter" in _refusal(_stdio_spec(argv=[shell, "-x"]), which=lambda c: f"/bin/{c}")


def test_inline_code_flags_are_refused() -> None:
    assert "inline" in _refusal(_stdio_spec(argv=[_PYTHON, "-c", "print(1)"]))


def test_a_bare_command_is_resolved_to_an_absolute_path() -> None:
    spec = _validated(
        _stdio_spec(argv=["uvx", "mcp-server-fetch"]), which=lambda c: f"/opt/bin/{c}"
    )
    assert spec.argv[0] == "/opt/bin/uvx"


def test_a_command_that_is_not_on_this_host_is_refused() -> None:
    assert "not found" in _refusal(_stdio_spec(argv=["no-such-mcp-server"]), which=lambda c: None)


def test_a_non_executable_file_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "server"
    plain.write_text("x")
    plain.chmod(0o644)
    assert "executable" in _refusal(_stdio_spec(argv=[str(plain)]))


def test_no_arbitrary_binaries_above_personal_without_an_allowlist() -> None:
    spec = _stdio_spec()
    assert "allowlist" in _refusal(spec, Tier.ENTERPRISE)
    assert "allowlist" in _refusal(spec, Tier.ENTERPRISE, stdio_allow=("/usr/bin/other",))
    allowed = _validated(spec, Tier.ENTERPRISE, stdio_allow=(_PYTHON,))
    assert allowed.argv[0] == _PYTHON


def test_federal_refuses_every_generated_bundle() -> None:
    message = _refusal(_stdio_spec(), Tier.FEDERAL, stdio_allow=(_PYTHON,))
    assert "federal" in message
    assert "federal" in _refusal(_http_spec(), Tier.FEDERAL)


def test_env_refs_cannot_steer_the_child() -> None:
    for variable in ("LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PATH", "NODE_OPTIONS", "bad name"):
        with pytest.raises(ExtensionError):
            _validated(_stdio_spec(env_refs={"api_key": variable}))


# --- tools: names, namespacing, shadowing ------------------------------------


@pytest.mark.parametrize("verb", ["", "has space", "a/b", "x" * 49, "rm;rf", "Δ"])
def test_a_tool_verb_must_be_a_plain_token(verb: str) -> None:
    with pytest.raises(ExtensionError):
        _validated(_http_spec(tools={verb: McpToolChoice()}))


def test_tool_names_are_namespaced_so_they_cannot_shadow_builtins() -> None:
    spec = _validated(_http_spec(tools={"bash": McpToolChoice(), "write": McpToolChoice()}))
    manifest = load_manifest(render_extension_toml(spec), tier=Tier.PERSONAL)
    assert manifest.tools.allow is not None
    assert sorted(manifest.tools.allow) == ["acme__bash", "acme__write"]
    assert not {"bash", "write"} & set(manifest.tools.allow)


def test_capability_tags_are_a_known_vocabulary() -> None:
    with pytest.raises(ExtensionError):
        _validated(_http_spec(tools={"x": McpToolChoice(capability_tags=["nonsense"])}))


# --- writing and signing ------------------------------------------------------


def _signer() -> tuple[InProcessSigner, bytes]:
    key = SigningKey.generate()
    return InProcessSigner(bytes(key)), bytes(key.verify_key)


def test_write_refuses_to_overwrite_without_replace(tmp_path: Path) -> None:
    spec = _validated(_http_spec())
    write_bundle(spec, tmp_path)
    with pytest.raises(ExtensionError):
        write_bundle(spec, tmp_path)
    write_bundle(spec, tmp_path, replace=True)


def test_write_refuses_a_name_another_root_already_holds(tmp_path: Path) -> None:
    shipped = tmp_path / "shipped"
    (shipped / "acme").mkdir(parents=True)
    with pytest.raises(ExtensionError) as caught:
        write_bundle(_validated(_http_spec()), tmp_path / "mine", other_roots=(shipped,))
    assert "already" in caught.value.message


def test_write_refuses_a_symlinked_bundle_directory(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "acme").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ExtensionError):
        write_bundle(_validated(_http_spec()), tmp_path, replace=True)
    assert not any(elsewhere.iterdir())


def test_write_leaves_nothing_group_or_world_writable(tmp_path: Path) -> None:
    folder = write_bundle(_validated(_http_spec()), tmp_path)
    for path in (folder, *folder.iterdir()):
        assert not stat.S_IMODE(path.stat().st_mode) & (stat.S_IWGRP | stat.S_IWOTH)


def test_every_file_signed_by_operator(tmp_path: Path) -> None:
    signer, public_key = _signer()
    folder = write_bundle(_validated(_http_spec()), tmp_path)
    signed = sign_bundle(folder, signer=signer, signer_did=_OPERATOR_DID)

    files = [p for p in folder.rglob("*") if p.is_file() and p.suffix != ".arcsig"]
    assert files and len(signed) == len(files)
    for path in files:
        assert artifact_signing.verify_file(path, path.read_bytes(), trusted_public_key=public_key)


async def test_a_tampered_generated_bundle_refuses_to_load_at_enterprise(tmp_path: Path) -> None:
    from arcagent.capabilities.capability_registry import CapabilityRegistry

    signer, public_key = _signer()
    spec = _validated(_stdio_spec(), Tier.ENTERPRISE, stdio_allow=(_PYTHON,))
    folder = write_bundle(spec, tmp_path)
    sign_bundle(folder, signer=signer, signer_did=_OPERATOR_DID)
    loader = ExtensionLoader(
        roots=(tmp_path / "extensions",),
        registry=CapabilityRegistry(),
        tier=Tier.ENTERPRISE,
        audit_sink=NullSink(),
        trusted_public_key=public_key,
        egress_allow=(),
    )
    await loader.load("localfs")

    manifest = folder / "extension.toml"
    manifest.write_text(manifest.read_text().replace("read_file", "delete_everything"))
    with pytest.raises(ExtensionError):
        await loader.load("localfs")


async def test_a_tampered_signed_bundle_is_refused_even_at_personal(tmp_path: Path) -> None:
    from arcagent.capabilities.capability_registry import CapabilityRegistry

    signer, public_key = _signer()
    folder = write_bundle(_validated(_http_spec()), tmp_path)
    sign_bundle(folder, signer=signer, signer_did=_OPERATOR_DID)
    (folder / "extra.py").write_text("raise SystemExit\n")
    loader = ExtensionLoader(
        roots=(tmp_path / "extensions",),
        registry=CapabilityRegistry(),
        tier=Tier.PERSONAL,
        audit_sink=NullSink(),
        trusted_public_key=public_key,
        egress_allow=(),
    )
    with pytest.raises(ExtensionError):
        await loader.load("acme")


# --- the digest ---------------------------------------------------------------


def test_spec_digest_is_stable_and_changes_with_any_field() -> None:
    first = spec_digest(_http_spec())
    assert first == spec_digest(_http_spec())
    assert first != spec_digest(_http_spec(url="https://mcp.acme.example/v2/mcp"))
    assert len(first) == 64 and all(c in "0123456789abcdef" for c in first)
