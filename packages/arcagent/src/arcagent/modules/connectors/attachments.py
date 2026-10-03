"""Build connector attachments and place only explicitly declared credentials."""

from __future__ import annotations

import contextlib
import importlib
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arcagent.core.errors import ExtensionError
from arcagent.extension.attachment import ExtensionAttachment, Requirement, RequirementKind
from arcagent.extension.cli_attachment import (
    CliAttachment,
    CliCommand,
    CliResilience,
    CredentialEnv,
)
from arcagent.extension.credential_broker import AccessTokenHandle
from arcagent.extension.environment import scrubbed_environment
from arcagent.extension.launcher import sandbox_policy_for
from arcagent.extension.manifest import ExtensionManifest
from arcagent.extension.mcp_policy import McpResilience, McpToolPolicy
from arcagent.extension.native_attachment import NativeAttachment
from arcagent.extension.secrets import Secret
from arcagent.extension.source import SourceAdapter
from arcagent.modules.connectors.credential_placement import (
    placement_environment,
    unplaced_secrets,
    visible_values,
)


def _refuse(message: str, **details: Any) -> ExtensionError:
    return ExtensionError(
        code="CONNECTOR_INSTALL_FAILED",
        message=f"probe: {message}",
        details={"step": "probe", **details},
    )


def _same_origin(url: str, origin: str) -> bool:
    """Whether ``url`` is on the same scheme + host + port as ``origin``.

    Structural, not a string prefix: a prefix match trusts a look-alike host such as
    ``https://vendor.example.com.evil.com/`` against ``https://vendor.example.com`` (no
    trailing slash) and a userinfo host such as ``https://vendor.example.com@evil.com/``.
    Comparing the parsed scheme and network location closes both regardless of how the
    ``url_origin`` allow-list happens to be written (SEC-01).
    """
    got, want = urlsplit(url), urlsplit(origin)
    return got.scheme == want.scheme and got.hostname == want.hostname and got.port == want.port


@contextlib.contextmanager
def _importable(bundle: Path) -> Iterator[None]:
    """Expose a bundle for exactly one native entrypoint resolution."""
    entry = str(bundle)
    sys.path.insert(0, entry)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(entry)


class _NativeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    entrypoint: str


class _CliConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    binary: str
    commands: list[CliCommand] = Field(default_factory=list)
    probe_argv: list[str] = Field(default_factory=lambda: ["--version"])
    install_instruction: str = ""
    resilience: CliResilience = Field(default_factory=CliResilience)
    #: Variable naming an empty per-connection config directory (``GH_CONFIG_DIR``).
    isolated_config_env: str = ""
    #: Fixed, non-credential variables every spawn gets (validated at manifest parse).
    static_env: dict[str, str] = Field(default_factory=dict)


class _McpStdioConfig(BaseModel):
    """A locally spawned MCP server: the transport launches a binary."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    transport: Literal["stdio"] = "stdio"
    argv: list[str] = Field(min_length=1)
    client_name: str = "arc"
    namespace: str = ""
    install_instruction: str = ""
    resilience: McpResilience = Field(default_factory=McpResilience)
    tools: dict[str, McpToolPolicy] = Field(default_factory=dict)


class _McpHttpConfig(BaseModel):
    """A hosted MCP server reached over HTTP: no binary, an https endpoint.

    ``credential_field`` names which supplied secret carries the token; an empty
    value means the endpoint needs none. By default the credential travels as an
    ``Authorization: Bearer`` header, but ``auth_header``/``auth_scheme`` let a
    vendor whose hosted MCP authenticates differently say so declaratively — e.g.
    Composio sends the raw key as ``x-api-key`` (``auth_scheme = ""``). The header
    is built as ``{auth_header: f"{auth_scheme} {token}".strip()}``, so an empty
    scheme yields the bare token. The credential never travels through environment
    placement, so the stdio ``[secrets.placement]`` requirement does not apply here.

    ``url_secret_field`` names which supplied secret carries an operator-minted URL
    that overrides ``url`` at connect time (Composio mints a per-user endpoint the
    operator pastes). ``url_origin``, when set, is the required https prefix of the
    effective URL: the trust-boundary guard that an operator cannot be tricked into
    pasting an attacker host in place of the vendor's real one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    transport: Literal["http"]
    url: str = Field(min_length=1)
    credential_field: str = ""
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    url_secret_field: str = ""
    url_origin: str = ""
    client_name: str = "arc"
    namespace: str = ""
    resilience: McpResilience = Field(default_factory=McpResilience)
    tools: dict[str, McpToolPolicy] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _operator_url_requires_an_origin_guard(self) -> _McpHttpConfig:
        """An operator-suppliable URL is unusable without a host allow-list.

        When ``url_secret_field`` lets the operator paste the endpoint, the connector's
        credential rides to whatever host was pasted. ``url_origin`` is the allow-list
        that keeps that host to the vendor's real one, so a manifest that offers the one
        without the other has no trust boundary at all — it must fail to load rather than
        ship an open credential-forwarding path (SEC-26).
        """
        if self.url_secret_field and not self.url_origin:
            raise ValueError(
                "url_origin is required when url_secret_field is set: an operator-supplied "
                "URL has no host allow-list without it"
            )
        return self


class _SourceConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    entrypoint: str


class _SourceEnabledAttachment:
    """Preserve an attachment's interactive hook while exposing its source adapter."""

    def __init__(self, delegate: ExtensionAttachment, source: SourceAdapter) -> None:
        self._delegate = delegate
        self._source = source

    def requirements(self) -> Any:
        return self._delegate.requirements()

    async def probe(self) -> Any:
        return await self._delegate.probe()

    async def describe_tools(self) -> Any:
        return await self._delegate.describe_tools()

    async def invoke(self, tool: str, args: dict[str, Any]) -> Any:
        return await self._delegate.invoke(tool, args)

    def source_adapter(self) -> SourceAdapter:
        return self._source


class _MultiSourceEnabledAttachment(_SourceEnabledAttachment):
    """One grant may expose multiple isolated document and message streams."""

    def __init__(self, delegate: ExtensionAttachment, sources: dict[str, SourceAdapter]) -> None:
        first = next(iter(sources.values()))
        super().__init__(delegate, first)
        self._sources = dict(sources)

    def source_adapters(self) -> dict[str, SourceAdapter]:
        return dict(self._sources)


def _visible(manifest: ExtensionManifest, secrets: Mapping[str, Secret]) -> dict[str, Secret]:
    """Only the fields the manifest declares non-sensitive."""
    sensitive = {declared.name for declared in manifest.secrets if declared.sensitive}
    return {name: value for name, value in secrets.items() if name not in sensitive}


def _placed_by_handle(
    manifest: ExtensionManifest, credential: AccessTokenHandle | None
) -> CredentialEnv | None:
    """A per-spawn resolver for the sensitive placed credentials, or None if there are none."""
    placed = [
        (declared.placement.variable, declared.name, declared.required)
        for declared in manifest.secrets
        if declared.sensitive and declared.placement is not None
    ]
    if not placed:
        return None
    if credential is None:
        raise _refuse(
            f"{manifest.extension.name} places a credential and was built with no "
            "credential handle",
            extension=manifest.extension.name,
        )
    handle = credential

    async def resolve() -> dict[str, Secret]:
        env: dict[str, Secret] = {}
        for variable, name, required in placed:
            value = await (handle.field(name) if required else handle.maybe_field(name))
            if value is not None:
                env[variable] = value
        return env

    return resolve


def build_attachment(
    manifest: ExtensionManifest,
    bundle: Path,
    secrets: Mapping[str, Secret],
    *,
    connection_id: str = "",
    download_dir: Path | None = None,
    credential: AccessTokenHandle | None = None,
    config_dir: Path | None = None,
) -> ExtensionAttachment:
    """Build the declared attachment at the sole credential-reveal boundary.

    ``credential`` is the connection's :class:`AccessTokenHandle` (P18-2). A
    ``native`` or ``cli`` attachment receives sensitive values ONLY through it, at
    call time: its build-time ``secrets`` are filtered to the fields the manifest
    declares non-sensitive, so a sensitive value is never baked into an
    attachment. ``mcp`` attachments still resolve their header/env at session
    start (C7) and get a reconcile push on every credential change.

    ``download_dir`` is where a CLI command that saves a file may write — the
    agent's own downloads folder for this connection. ``None`` (every management
    surface) refuses every download.

    ``connection_id`` names WHICH account this is. An extension that keeps
    per-connection operator configuration — the semantic layer describing one
    datastore's tables — has no other way to find its own file: two connections
    to the same extension are two different databases with two different
    meanings, and a bundle path is the same for both.
    """
    kind = manifest.extension.attachment
    if kind == "native":
        entrypoint = _NativeConfig.model_validate(manifest.config.get("native", {})).entrypoint
        visible = visible_values(manifest, secrets)
        context: dict[str, Any] = {"bundle": str(bundle), "connection_id": connection_id}
        context.update(visible)
        context["credential"] = credential
        context["download_dir"] = str(download_dir) if download_dir is not None else ""
        with _importable(bundle):
            native = NativeAttachment(entrypoint, context)
        return _with_source_adapter(
            manifest, bundle, native, {"connection_id": connection_id, **visible}
        )
    if kind == "cli":
        unplaced = unplaced_secrets(manifest)
        if unplaced:
            raise _refuse(
                f"{manifest.extension.name} declares credential(s) {', '.join(unplaced)} "
                "with no [secrets.placement], and attaches as 'cli', which can only "
                "deliver a credential the bundle names a destination for",
                extension=manifest.extension.name,
                attachment=kind,
                unplaced=unplaced,
            )
        declared = _CliConfig.model_validate(manifest.config.get("cli", {}))
        cli_attachment = CliAttachment(
            binary=declared.binary,
            commands=declared.commands,
            probe_argv=declared.probe_argv,
            install_instruction=declared.install_instruction,
            resilience=declared.resilience,
            env=placement_environment(manifest, _visible(manifest, secrets)),
            values=visible_values(manifest, secrets),
            owned_env=frozenset(
                declared.placement.variable
                for declared in manifest.secrets
                if declared.placement is not None
            ),
            visible_env=frozenset(
                declared.placement.variable
                for declared in manifest.secrets
                if declared.placement is not None and not declared.sensitive
            ),
            download_dir=download_dir,
            credential_env=_placed_by_handle(manifest, credential),
            static_env=declared.static_env,
            isolated_config_env=declared.isolated_config_env,
            config_dir=config_dir,
        )
        return _with_source_adapter(
            manifest,
            bundle,
            cli_attachment,
            {"connection_id": connection_id, **visible_values(manifest, secrets)},
        )
    if kind == "mcp":
        from arcagent.extension.mcp_attachment import SdkMcpClient

        mcp_raw = manifest.config.get("mcp", {})
        if mcp_raw.get("transport") == "http":
            http_config = _McpHttpConfig.model_validate(mcp_raw)
            # The endpoint may be operator-minted (a per-user URL the operator pastes
            # in) rather than pinned in the manifest. Take the supplied override when
            # the bundle names its channel, then validate the effective URL is on the
            # vendor's required origin so a stolen/typo host cannot be substituted.
            url = http_config.url
            if http_config.url_secret_field:
                supplied_url = secrets.get(http_config.url_secret_field)
                if supplied_url is not None:
                    url = supplied_url.reveal()
            if http_config.url_origin and not _same_origin(url, http_config.url_origin):
                raise _refuse(
                    f"the MCP endpoint {url!r} is not on the required origin "
                    f"{http_config.url_origin!r}",
                    extension=manifest.extension.name,
                    attachment=kind,
                )
            token = (
                secrets.get(http_config.credential_field) if http_config.credential_field else None
            )
            # The credential is revealed into the header mapping the SDK transport
            # carries. Most hosted MCP servers take an Authorization: Bearer header;
            # a vendor that authenticates differently declares auth_header/auth_scheme
            # (an empty scheme sends the bare token, e.g. Composio's x-api-key).
            headers = (
                {http_config.auth_header: f"{http_config.auth_scheme} {token.reveal()}".strip()}
                if token is not None
                else None
            )
            requirements = (
                [
                    Requirement(
                        kind=RequirementKind.CREDENTIAL,
                        name=http_config.credential_field,
                        instruction=f"Provide the credential for {url}",
                    )
                ]
                if http_config.credential_field
                else []
            )
            mcp_attachment = SdkMcpClient.for_http(
                url=url,
                headers=headers,
                tools=http_config.tools,
                resilience=http_config.resilience,
                client_name=http_config.client_name,
                requirements=requirements,
            )
            return _with_source_adapter(
                manifest, bundle, _namespaced(mcp_attachment, http_config.namespace)
            )

        unplaced = unplaced_secrets(manifest)
        if unplaced:
            raise _refuse(
                f"{manifest.extension.name} declares credential(s) {', '.join(unplaced)} "
                "with no [secrets.placement], and attaches as 'mcp', which can only "
                "deliver a credential the bundle names a destination for",
                extension=manifest.extension.name,
                attachment=kind,
                unplaced=unplaced,
            )
        stdio_config = _McpStdioConfig.model_validate(mcp_raw)
        # Preserve the two controls the ProcessLauncher applied to the spawn: the
        # deployment sandbox policy still wraps the argv (a no-op at personal tier,
        # the confinement wrapper at a stricter one), and the child's environment is
        # still scrubbed of loader/interpreter-startup variables (REQ-273) before it
        # reaches the SDK's StdioServerParameters.env.
        declared_env = {
            name: secret.reveal()
            for name, secret in placement_environment(manifest, secrets).items()
        }
        policy = sandbox_policy_for(manifest.extension.tier_floor)
        wrapped_argv = policy.wrap(list(stdio_config.argv))
        mcp_attachment = SdkMcpClient.for_stdio(
            command=wrapped_argv[0],
            args=wrapped_argv[1:],
            env=scrubbed_environment(declared_env),
            tools=stdio_config.tools,
            resilience=stdio_config.resilience,
            client_name=stdio_config.client_name,
            requirements=[
                Requirement(
                    kind=RequirementKind.HOST,
                    name=stdio_config.argv[0],
                    instruction=stdio_config.install_instruction,
                )
            ],
        )
        return _with_source_adapter(
            manifest, bundle, _namespaced(mcp_attachment, stdio_config.namespace)
        )
    raise _refuse(f"unknown attachment kind {kind!r}", attachment=kind)


class _NamespacedAttachment:
    """Serve a server's tools as ``<namespace>__<verb>``, so none can shadow a built-in.

    An MCP server names its own tools, and a bare ``bash`` or ``write`` from a server
    an operator added would otherwise compete with the built-in of that name. The
    prefix is applied at this one seam: the manifest declares, allows and classifies
    the prefixed names, and the call is stripped back to the server's own verb on the
    way out. A call for a name outside the namespace never reaches the server.
    """

    def __init__(self, delegate: ExtensionAttachment, namespace: str) -> None:
        self._delegate = delegate
        self._prefix = f"{namespace}__"

    def requirements(self) -> Any:
        return self._delegate.requirements()

    async def probe(self) -> Any:
        result = await self._delegate.probe()
        return result.model_copy(update={"tools": self._renamed(result.tools)})

    async def describe_tools(self) -> Any:
        return self._renamed(await self._delegate.describe_tools())

    async def invoke(self, tool: str, args: dict[str, Any]) -> Any:
        if not tool.startswith(self._prefix):
            raise _refuse(f"{tool!r} is outside this server's namespace", tool=tool)
        result = await self._delegate.invoke(tool.removeprefix(self._prefix), args)
        return result.model_copy(update={"tool": tool})

    def _renamed(self, specs: Any) -> list[Any]:
        return [spec.model_copy(update={"name": f"{self._prefix}{spec.name}"}) for spec in specs]


def _namespaced(attachment: ExtensionAttachment, namespace: str) -> ExtensionAttachment:
    return _NamespacedAttachment(attachment, namespace) if namespace else attachment


def _with_source_adapter(
    manifest: ExtensionManifest,
    bundle: Path,
    attachment: ExtensionAttachment,
    identity: Mapping[str, str] | None = None,
) -> ExtensionAttachment:
    """Wrap ``attachment`` with the bundle's ``[config.source]`` adapter, if it declares one.

    ``identity`` (the connection id and the non-sensitive fields) lets an adapter
    name WHICH account it syncs, so two mailboxes are two sources.
    """
    source_config = manifest.config.get("source")
    if source_config is None:
        return attachment
    entrypoint = _SourceConfig.model_validate(source_config).entrypoint
    context: dict[str, Any] = {**(identity or {}), "attachment": attachment}
    with _importable(bundle):
        module = importlib.import_module(entrypoint)
        factories = getattr(module, "build_source_adapters", None)
        if callable(factories):
            sources = factories(context)
            if (
                not isinstance(sources, dict)
                or not sources
                or not all(isinstance(adapter, SourceAdapter) for adapter in sources.values())
            ):
                raise _refuse(
                    "source entrypoint did not return SourceAdapters", entrypoint=entrypoint
                )
            return _MultiSourceEnabledAttachment(attachment, sources)
        factory = getattr(module, "build_source_adapter", None)
        source = factory(context) if callable(factory) else None
    if not isinstance(source, SourceAdapter):
        raise _refuse("source entrypoint did not return a SourceAdapter", entrypoint=entrypoint)
    return _SourceEnabledAttachment(attachment, source)


__all__ = ["build_attachment", "placement_environment", "visible_values"]
