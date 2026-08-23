"""SPEC-062 COMP-001 — ``extension.toml``, the only document Arc parses from an extension.

An extension is third-party, lower-trust code, so every claim its manifest makes is
adversarial input. Four controls keep the document from becoming a
privilege-escalation form:

* ``extra="forbid"`` — a typo raises and names the offending key rather than being
  silently ignored (the ``core/module_config.py`` precedent).
* denied keys are **stripped**, not honoured — a manifest cannot reach the vault
  backend, native process tools, the tool preamble, the sandbox path floor, or
  identity key custody (the ``_DENIED_OVERLAY_PATHS`` precedent in
  ``arccli/blueprints.py``).
* an unbounded tool allowlist is refused above personal tier (REQ-268); omitting the
  allowlist is the same unbounded grant as asking for ``*``.
* a third-party artifact is pinned to one exact version **and** a sha256 for every
  platform it publishes (REQ-290). A floating pin is a supply-chain hole, not a
  convenience — and so is one digest standing in for every platform, which matches
  exactly one machine and describes the wrong bytes on all the others.
* a declared tier floor may only **refuse** to load below that tier — it can never raise
  the deployment's effective stringency (D-579). The operator sets the tier; a bundle
  author may decline to run under it, never redefine it.

Name validation is deliberately absent: :mod:`arcagent.extension.catalog` owns it, and
it is the component that turns a name into a filesystem path.
"""

from __future__ import annotations

import copy
import logging
import re
import tomllib
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.attachment import Classification
from arcagent.extension.environment import refuses_placement
from arcagent.extension.field_formats import SuppliedFormat
from arcagent.extension.platforms import ANY_PLATFORM
from arcagent.tiers import tier_rank

_logger = logging.getLogger("arcagent.extension.manifest")

#: Trusted-admin-only config paths a manifest must never set. Mirrors
#: ``_DENIED_OVERLAY_PATHS`` (``arccli/blueprints.py``): a lower-trust bundle must not
#: touch the vault backend, native tool execution, the tool preamble, the sandbox
#: filesystem floor (SEC-18), or identity key custody.
_DENIED_CONFIG_PATHS: tuple[tuple[str, ...], ...] = (
    ("vault", "backend"),
    ("tools", "process"),
    ("tools", "preamble"),
    ("tools", "policy", "allowed_paths"),
    ("identity", "key_dir"),
)

#: An exact build: starts with a digit, carries no comparator and no wildcard. Rejects
#: ">=2.3", "latest", and "2.*" — each of which lets the executed bytes change later.
_EXACT_VERSION = r"^[0-9][0-9a-zA-Z.+_-]*$"

#: A full sha256 digest, lowercase hex.
_SHA256 = r"^[0-9a-f]{64}$"

#: Artifacts are fetched over TLS only. The digest already pins the bytes, but a
#: plaintext URL hands a network attacker the ability to choose which refusal an
#: operator sees, and there is no published release that needs it.
_HTTPS_URL = r"^https://"

#: The allowlist entry that asks for every tool the upstream cares to serve.
_WILDCARD = "*"

#: A POSIX environment name. A placement becomes an entry in a spawned child's
#: environment, and anything outside this either cannot be exported or is folded onto a
#: neighbouring name — both of which deliver the credential nowhere.
_ENV_NAME = r"^[A-Z][A-Z0-9_]*$"


def _strip_denied(config: dict[str, Any]) -> dict[str, Any]:
    """Drop trusted-admin-only keys a manifest must not set (see ``_DENIED_CONFIG_PATHS``)."""
    result = copy.deepcopy(config)
    for path in _DENIED_CONFIG_PATHS:
        node: dict[str, Any] | None = result
        for part in path[:-1]:
            nxt = node.get(part) if node is not None else None
            node = nxt if isinstance(nxt, dict) else None
        if node is not None and path[-1] in node:
            node.pop(path[-1])
            _logger.warning(
                "extension manifest set trusted-admin key %s; ignoring it", ".".join(path)
            )
    return result


class _ManifestModel(BaseModel):
    """Base for every manifest table: an unknown key is a hard error, never a shrug."""

    model_config = ConfigDict(extra="forbid")


class ExtensionHeader(_ManifestModel):
    """``[extension]`` — who this bundle is and how it attaches.

    ``attachment`` is an open string on purpose: a third party ships a new attachment
    kind by providing an implementation, and a closed enum here would make that a core
    edit (REQ-278/REQ-280). The loader resolves the kind and refuses an unknown one.
    """

    name: str
    version: str
    #: The product's own name, spelled the way its vendor spells it — with the
    #: capitals, digits and spaces a real product name has. ``name`` is a
    #: COORDINATE: it is validated, and config keys, secret refs, CLI arguments and
    #: paths are built from it, so it can carry none of those. Every surface was
    #: therefore showing an operator a lowercased identifier rather than the name
    #: they know the product by. Optional, and empty means a surface falls back to
    #: the coordinate rather than rendering a blank row.
    display_name: str = ""
    #: One line, written for a person choosing from a list: what connecting this
    #: lets the agent do. Read by ``ExtensionCatalog.available`` and shown by every
    #: picker, so a bundle without one is a row an operator cannot choose from.
    description: str = ""
    attachment: str = Field(min_length=1)
    #: The weakest deployment this bundle agrees to run in. Read ONLY by
    #: :func:`load_manifest`'s refusal, and by nothing that resolves policy —
    #: which is what keeps a bundle author from raising the operator's tier.
    tier_floor: Tier = Tier.PERSONAL

    @property
    def label(self) -> str:
        """What to show a person. Never empty, and never a substitute for ``name``."""
        return self.display_name or self.name


class PlatformArtifact(_ManifestModel):
    """``[artifact.platforms."<os>/<arch>"]`` — one platform's published build.

    ``member`` is the path INSIDE the downloaded archive of the executable to
    place on PATH. It is empty when the download is not an archive holding one
    binary — an npm tarball, a PyPI sdist — which is a build Arc can still pin
    and still cannot install, and says so rather than guessing.
    """

    url: str = Field(pattern=_HTTPS_URL)
    sha256: str = Field(pattern=_SHA256)
    member: str = ""


class ArtifactPin(_ManifestModel):
    """``[artifact]`` — the exact third-party build Arc will execute (REQ-290).

    Digests are keyed by platform, spelled as :func:`~arcagent.extension.
    platforms.host_platform` spells it, because a sha256 only describes the bytes
    of the asset it was published beside. One digest for the whole pin would be
    correct on the platform it was taken from and wrong everywhere else, leaving
    an installer the choice of skipping verification or refusing every host but
    one. A platform this pin does not name has no answer here, which is what lets
    a caller refuse that host by name.
    """

    package: str
    version: str = Field(pattern=_EXACT_VERSION)
    platforms: dict[str, PlatformArtifact] = Field(min_length=1)

    def for_host(self, host: str) -> PlatformArtifact | None:
        """The build pinned for ``host``, the platform-independent one, or ``None``."""
        return self.platforms.get(host) or self.platforms.get(ANY_PLATFORM)


class HostRequirement(_ManifestModel):
    """``[[host_requires]]`` — a prerequisite the operator installs on the host (REQ-262).

    ``authorize_command`` is first-class rather than a line an operator has to find
    inside ``instruction``: a bundle that declares no ``[[secrets]]`` holds its
    credential in the binary's own keyring, so this string is the entire answer to
    "how do I connect this?" and a surface has to be able to show it on its own.
    Empty means the binary is a runtime with no account of its own — Node, Python —
    and there is nothing to authorise.

    ``token_command`` is the same login run to completion WITHOUT a human: it reads
    the token on stdin and exits. Most CLI logins are not that — ``gog auth add``
    opens a browser, ``ms-365-mcp-server --login`` prints a device code — and a
    surface that offered a button for those would either hang on a prompt or claim
    a sign-in that never happened. Empty is the honest "only a person at this host
    can finish it", and it is the default, because silence must never read as
    "try it and see".

    ``verify_command`` answers the question the probe does not: not "does this
    program run" but "is this account connected". ``dbxcli version`` succeeds on a
    ``dbxcli`` holding no credential at all, and rendering that success as
    **Signed in** told an operator a security-relevant thing was done when it was
    not. ``verify_pattern`` is a regex the output must match for the two states
    that a command's exit code cannot tell apart — ``gog auth list`` exits zero
    with an empty listing, ``ms-365-mcp-server --verify-login`` exits zero and
    prints ``{"success":false}``. Empty ``verify_command`` means Arc cannot tell,
    which every surface must render as unknown: never as signed in, which is the
    defect, and never as signed out, which sends an operator to redo a login they
    already completed.
    """

    name: str
    minimum_version: str | None = None
    instruction: str = ""
    authorize_command: str = ""
    token_command: str = ""
    verify_command: str = ""
    verify_pattern: str = ""

    @field_validator("verify_pattern")
    @classmethod
    def _pattern_must_compile(cls, pattern: str) -> str:
        """A pattern that will not compile must not become a runtime verdict.

        Refused here rather than caught at check time: either fallback would be a
        lie — "signed out" sends an operator to fix nothing, "signed in" is the
        defect this field exists to close.
        """
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"verify_pattern is not a valid regex: {exc}") from exc
        return pattern

    @model_validator(mode="after")
    def _pattern_needs_a_command(self) -> HostRequirement:
        """A pattern nothing runs is a control an operator believes is in force."""
        if self.verify_pattern and not self.verify_command:
            raise ValueError("verify_pattern is set but no verify_command runs it")
        return self


class CredentialPlacement(_ManifestModel):
    """``[secrets.placement]`` — where this bundle's own tool reads this credential.

    The seam that lets a connector whose tool holds its own credential be finished
    by pasting one. Before it, a ``cli`` bundle declaring ``[[secrets]]`` was refused
    outright, because no table said which of that binary's inputs a credential would
    become — so storing one would have delivered it nowhere.

    ``variable`` is the environment name the *bundle's tool* reads, and it is the
    bundle's knowledge alone: which variable a given binary takes its credential from
    is declared in that bundle's manifest and moves when its upstream moves it, so an
    upstream change is a one-bundle fix and never a core edit. Arc exports the stored
    value into every process that attachment starts and nowhere else — never onto argv,
    which is the process table, and never onto the host filesystem, so two connected
    accounts of one bundle stay separate rather than sharing whatever single file the
    upstream's own login would have written.

    A name that would steer the child rather than carry a value is refused
    (:func:`~arcagent.extension.environment.refuses_placement`): ``PATH`` chooses which
    binary runs, and the loader and interpreter families are scrubbed on the way in, so
    a placement naming one would parse, place nothing, and still be reported as done.
    """

    variable: str = Field(pattern=_ENV_NAME)

    @field_validator("variable")
    @classmethod
    def _must_only_carry_a_value(cls, name: str) -> str:
        if refuses_placement(name):
            raise ValueError(
                f"{name} decides what the process runs or loads and cannot carry a credential"
            )
        return name


class SecretRequirement(_ManifestModel):
    """``[[secrets]]`` — a credential the operator supplies, and under which key.

    ``placement`` is optional and its absence is not an error: a bundle whose own
    adapter code receives the value has nothing to place. It is required only of a
    bundle whose attachment reaches its service by starting a program, because that is
    the only shape with somewhere to put it.

    ``sensitive`` says whether the value is actually a credential. Several of these
    entries are configuration — a base URL, an account address, a tenant identifier —
    and a form that masked them bought no protection while costing an operator the
    ability to see whether they had typed a URL correctly. It defaults to true so a
    field that forgets to say is masked; declaring ``sensitive = false`` is a bundle
    author's deliberate act, made in the bundle, because core has no business
    guessing which of a service's fields happen to be secret.

    ``format`` is the SHAPE the value must be in, put there where it enters
    (:func:`~arcagent.extension.field_formats.normalize`). A person types a web
    address without a scheme, because that is what a browser bar shows them;
    knowing it needs one is the tool's job. Empty means the value is stored exactly
    as typed, which is the right answer for a token whose characters are all
    significant.
    """

    name: str
    prompt: str = ""
    placement: CredentialPlacement | None = None
    sensitive: bool = True
    format: SuppliedFormat = ""


class OAuthFlow(_ManifestModel):
    """``[oauth]`` — a native OAuth2 authorization-code connect flow, done in-harness.

    The host-login path signs in a host BINARY that owns its own credential. A
    native connector (no binary) that speaks OAuth2 has no such path, so without
    this the operator has to obtain a refresh token by hand — running the
    code→token exchange themselves. Declaring this block lets ``arc connector
    authorize`` do the whole sign-in: build the provider's authorize URL from the
    stored client id, take the one-time code the provider shows, exchange it for a
    durable refresh token, and store that under ``refresh_token_secret``. The
    operator never obtains, types, or sees a refresh token, and no adapter
    reimplements the exchange.

    ``client_id_secret`` / ``client_secret_secret`` name the ``[[secrets]]`` the
    operator supplies (the app key and secret). ``refresh_token_secret`` names the
    ``[[secrets]]`` this flow WRITES — since the operator never types it, that
    secret's entry carries no prompt.
    """

    authorize_url: str
    token_url: str
    client_id_secret: str
    client_secret_secret: str
    refresh_token_secret: str
    authorize_params: dict[str, str] = Field(default_factory=dict)


class DeclaredTool(_ManifestModel):
    """``[[tools.declared]]`` — one tool's classification and trifecta capability tags."""

    name: str
    description: str = ""
    classification: Classification = "state_modifying"
    capability_tags: list[str] = Field(default_factory=list)


class ToolPolicy(_ManifestModel):
    """``[tools]`` — which tools may register, and what each one is."""

    allow: list[str] | None = None
    declared: list[DeclaredTool] = Field(default_factory=list)

    @property
    def is_unbounded(self) -> bool:
        """True when the manifest declares no bound on which tools may register.

        An omitted ``allow`` is the same unbounded grant as ``["*"]``: whatever the
        upstream serves, registers. The refusal must not be escapable by silence.
        """
        return self.allow is None or _WILDCARD in self.allow


class ApprovalPolicy(_ManifestModel):
    """``[approval]`` — the default human-approval mode for this extension (REQ-274).

    ``outbound`` admits inbound reads and gates every outbound call; an operator may
    relax it to ``none`` for a named connected account, or tighten it to ``all``.
    """

    default: Literal["none", "outbound", "all"] = "outbound"


class KnowledgeDeclaration(_ManifestModel):
    """How a connector participates in the canonical Knowledge lifecycle."""

    mode: Literal["source", "non_indexable"]
    reason: str = ""

    @model_validator(mode="after")
    def _non_indexable_needs_reason(self) -> KnowledgeDeclaration:
        if self.mode == "non_indexable" and not self.reason.strip():
            raise ValueError("non_indexable knowledge declarations require a reason")
        return self


#: A bundle field named inside a declared command. Deliberately narrow — the same
#: shape a ``[[secrets]]`` name has — so nothing else in a command reads as one.
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def placeholders(command: str) -> list[str]:
    """The bundle fields a declared command names, in the order they first appear.

    Public because three call sites read the same rule: the manifest refuses a name
    that is wrong, the install path asks which fields a login already delivers, and
    the login fills them in. Three copies is three places for the rule to drift.
    """
    return list(dict.fromkeys(_PLACEHOLDER.findall(command)))


def fill_placeholders(token: str, values: Mapping[str, str]) -> str:
    """One argv token with its named fields filled in — a single pass, never nested.

    One pass matters: a value that happens to look like a placeholder is left as the
    operator typed it rather than being expanded into a field it was not given.
    """
    return _PLACEHOLDER.sub(lambda found: values.get(found[1], found[0]), token)


class ExtensionManifest(_ManifestModel):
    """A parsed, denied-key-stripped ``extension.toml``."""

    extension: ExtensionHeader
    artifact: ArtifactPin | None = None
    host_requires: list[HostRequirement] = Field(default_factory=list)
    secrets: list[SecretRequirement] = Field(default_factory=list)
    oauth: OAuthFlow | None = None
    requires: list[str] = Field(default_factory=list)
    tools: ToolPolicy = Field(default_factory=ToolPolicy)
    approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    knowledge: KnowledgeDeclaration = Field(
        default_factory=lambda: KnowledgeDeclaration(mode="source")
    )
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("config")
    @classmethod
    def _drop_denied_keys(cls, config: dict[str, Any]) -> dict[str, Any]:
        return _strip_denied(config)

    @model_validator(mode="after")
    def _oauth_names_declared_secrets(self) -> ExtensionManifest:
        """An ``[oauth]`` flow may only wire ``[[secrets]]`` this bundle declares.

        The flow reads the client id/secret from the store and writes the refresh
        token back to it; naming a field the bundle never declared would fail at
        connect with a store miss instead of here, where the author can see it.
        """
        if self.oauth is None:
            return self
        declared = {secret.name for secret in self.secrets}
        for role, name in (
            ("client_id_secret", self.oauth.client_id_secret),
            ("client_secret_secret", self.oauth.client_secret_secret),
            ("refresh_token_secret", self.oauth.refresh_token_secret),
        ):
            if name not in declared:
                raise ValueError(
                    f"[oauth].{role} = {name!r} names no [[secrets]] this bundle declares"
                )
        return self

    @model_validator(mode="after")
    def _a_declared_command_may_only_name_this_bundles_visible_fields(self) -> ExtensionManifest:
        """A command names fields, and both ways of naming a wrong one lie.

        A field the bundle never declares is never supplied, so the command would run
        with a literal ``{region}`` and fail in the binary's words about a flag nobody
        set. A field that IS a credential is worse: the command becomes a route onto
        argv, which is the process table every other user on the box can read — and the
        one thing this whole path exists to prevent is a credential going anywhere but
        stdin or the child's environment.

        Both places a manifest can write one are checked here, because they are the
        same mistake: a ``token_command``, and the fixed ``argv`` of a declared CLI
        command (where a bundle puts a blast-radius value the model must not choose).
        """
        visible = {declared.name for declared in self.secrets if not declared.sensitive}
        declared_names = {declared.name for declared in self.secrets}
        for source, command in self._commands_naming_fields():
            for field in placeholders(command):
                if field in visible:
                    continue
                reason = (
                    "is a credential and may never reach argv"
                    if field in declared_names
                    else "is not a field this bundle declares"
                )
                raise ValueError(f"{source} names '{field}', which {reason}")
        return self

    def fields_named_by_commands(self) -> set[str]:
        """Every declared field some command carries on its own argv or stdin.

        A field here HAS a destination even with no ``[secrets.placement]``: the
        command delivers it. Read by the install path, which otherwise refuses a
        spawning bundle's unplaced credential — correctly, since a stored value
        nothing delivers is the defect that rule exists for.
        """
        return {
            field
            for _, command in self._commands_naming_fields()
            for field in placeholders(command)
        }

    def _commands_naming_fields(self) -> list[tuple[str, str]]:
        """Every manifest string that may name a field, labelled for the refusal."""
        sources = [
            (f"{required.name}'s token_command", required.token_command)
            for required in self.host_requires
        ]
        declared: Any = self.config.get("cli", {}).get("commands", [])
        for command in declared if isinstance(declared, list) else []:
            tool = command.get("tool", "a command") if isinstance(command, dict) else "a command"
            argv = command.get("argv", []) if isinstance(command, dict) else []
            sources += [(f"{tool}'s argv", token) for token in argv if isinstance(token, str)]
        return sources


def load_manifest(text: str, *, tier: Tier) -> ExtensionManifest:
    """Parse ``extension.toml`` and apply the tier-gated refusals.

    Args:
        text: The manifest source.
        tier: The deployment tier the extension would load into. Returned
            unchanged in effect — nothing here can strengthen it (D-579).

    Returns:
        The validated manifest.

    Raises:
        ValidationError: An unknown key, a missing table, or an unpinned artifact.
        ExtensionError: The manifest asks for an unbounded tool allowlist above
            personal tier (REQ-268), or declares a tier floor above ``tier``.
    """
    manifest = ExtensionManifest.model_validate(tomllib.loads(text))
    floor = manifest.extension.tier_floor
    if tier_rank(tier) < tier_rank(floor):
        raise ExtensionError(
            code="EXTENSION_REFUSED",
            message=(
                f"extension '{manifest.extension.name}' declares a {floor} tier floor "
                f"and will not load at {tier} tier"
            ),
            details={"reason": "tier_floor", "tier": str(tier), "tier_floor": str(floor)},
        )
    if manifest.tools.is_unbounded and tier is not Tier.PERSONAL:
        raise ExtensionError(
            code="EXTENSION_REFUSED",
            message=(
                f"extension '{manifest.extension.name}' requests an unbounded tool "
                f"allowlist, which is refused at {tier} tier"
            ),
            details={"reason": "unbounded_tool_allowlist", "tier": str(tier)},
        )
    return manifest


__all__ = [
    "ApprovalPolicy",
    "ArtifactPin",
    "CredentialPlacement",
    "DeclaredTool",
    "ExtensionHeader",
    "ExtensionManifest",
    "HostRequirement",
    "KnowledgeDeclaration",
    "PlatformArtifact",
    "SecretRequirement",
    "ToolPolicy",
    "fill_placeholders",
    "load_manifest",
    "placeholders",
]
