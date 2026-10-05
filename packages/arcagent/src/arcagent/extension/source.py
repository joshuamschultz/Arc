"""Vendor-neutral seam for incrementally synchronized connected data sources."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from arcagent.extension.credentials import CredentialRenewalError


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceObjectKind(StrEnum):
    """The portable shape of one object discovered at a source."""

    FILE = "file"
    FOLDER = "folder"
    DELETED = "deleted"


class SourceDataShape(StrEnum):
    """Vendor-neutral information shape used to choose an ingestion path."""

    DOCUMENT = "document"
    MAIL = "mail"
    DATASTORE = "datastore"
    BLOB = "blob"
    PROFILE = "profile"


class SourceFailureCode(StrEnum):
    """Failures an orchestrator can act on without knowing the vendor."""

    AUTH_REQUIRED = "auth_required"
    RATE_LIMITED = "rate_limited"
    CHECKPOINT_INVALID = "checkpoint_invalid"
    TOO_LARGE = "too_large"
    NOT_FOUND = "not_found"
    #: The provider's API is switched off for the operator's own cloud project. Only
    #: the operator can enable it, so it is never retried.
    API_DISABLED = "api_disabled"
    VERSION_CHANGED = "version_changed"
    UNSUPPORTED_CONTENT = "unsupported_content"
    TRANSIENT = "transient"


class SourceError(RuntimeError):
    """A typed source refusal with an optional safe retry delay.

    ``action_url`` is where an operator fixes it, when the provider named a place
    we trust (see :func:`api_disabled_link`); never a URL taken on faith.
    """

    def __init__(
        self,
        code: SourceFailureCode,
        detail: str,
        *,
        retry_after: float | None = None,
        action_url: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.retry_after = retry_after
        self.action_url = action_url


#: What a vendor CLI or a provider says, in prose, when nobody is signed in. This
#: is the only signal for text Arc did not produce: a vendor CLI's stderr or a
#: provider's error body carries no code the wrapper passes through. Arc's OWN
#: credential failures never come through here. They are typed
#: (``CredentialRenewalError``) and mapped by :func:`source_error_from_renewal`,
#: so a reworded message cannot turn a missing credential into a retry storm.
#: "keyring" and "tty" are here because a headless box that cannot unlock the
#: vendor's credential store is signed out for every practical purpose: only a
#: person at a terminal can fix it, and retrying changes nothing.
_AUTH_MARKERS = (
    "invalid_grant",
    "expired or revoked",
    "token has been expired",
    "no auth for",
    "not authenticated",
    "authentication failed",
    "authentication required",
    "requires authentication",
    "failed to authenticate",
    "not logged in",
    "login required",
    "bad credentials",
    "unauthorized",
    "keyring",
    "no tty",
    "not a tty",
    "without a terminal",
)

#: A bare status number is only a status when nothing word-like touches it:
#: ``PROJ-401`` is an issue key, not an authorization failure.
_AUTH_STATUS = re.compile(r"(?<![\w-])401(?![\w-])")
_RATE_STATUS = re.compile(r"(?<![\w-])429(?![\w-])")
_RATE_MARKERS = ("rate limit", "ratelimitexceeded", "secondary rate")


#: What a provider says when an API is off for the operator's project. Google's
#: error reason is ``accessNotConfigured``; its message says "has not been used in
#: project ... or it is disabled".
_API_DISABLED_MARKERS = ("accessnotconfigured", "has not been used in project")
#: The only hosts an "enable this API" link may point at. A provider error body is
#: attacker-influenced text, and the link becomes a button an operator clicks.
_CONSOLE_HOSTS = frozenset({"console.developers.google.com", "console.cloud.google.com"})
_ANY_URL = re.compile(r"https?://[^\s<>\"']+")
_API_NAME = re.compile(r"(Google [A-Za-z ]{2,40}? API)(?= has not been used| is not enabled)")


def api_disabled_link(text: str) -> str | None:
    """The Google Cloud console link in ``text``, only if it is a real console URL.

    Anything else (another host, plain http, a look-alike host, userinfo, a
    non-URL scheme) is dropped: the operator is told to enable the API, with no
    link, rather than handed one a hostile error body chose.
    """
    for match in _ANY_URL.finditer(text):
        candidate = match.group(0).rstrip(".,;:)]}")
        parts = urlsplit(candidate)
        if (
            parts.scheme == "https"
            and parts.hostname in _CONSOLE_HOSTS
            and parts.username is None
            and parts.port is None
        ):
            return candidate
    return None


def api_disabled_name(text: str) -> str:
    """The API's name as the provider's sentence words it, or a generic fallback."""
    found = _API_NAME.search(text)
    return found.group(1) if found else "Google API"


def source_error_from_renewal(exc: CredentialRenewalError) -> SourceError:
    """Map Arc's typed credential failure to a source failure, by type and not by prose.

    A credential only a person can fix (missing, revoked, consent withdrawn) is
    ``AUTH_REQUIRED``, which the orchestrator never retries and the connection
    health authority turns into ``needs_you``. Any other renewal failure (the
    provider's token endpoint was down) is ``TRANSIENT`` and keeps its retry delay.
    """
    if exc.terminal:
        return SourceError(SourceFailureCode.AUTH_REQUIRED, exc.message[:256])
    return SourceError(SourceFailureCode.TRANSIENT, exc.message[:256], retry_after=exc.retry_after)


def classify_cli_failure(detail: str) -> SourceFailureCode:
    """Classify a vendor-CLI failure so the orchestrator can act on it.

    Credential trouble is terminal (only a person can fix it), a rate limit means
    wait, and anything else is TRANSIENT: a bounded retry, and now a bounded run of
    consecutive failures, rather than a list of network phrasings to keep current.
    Judge the WHOLE message; a vendor puts the reason at the end, after a URL.
    """
    lowered = detail.lower()
    if any(marker in lowered for marker in _API_DISABLED_MARKERS):
        return SourceFailureCode.API_DISABLED
    if any(marker in lowered for marker in _AUTH_MARKERS) or _AUTH_STATUS.search(lowered):
        return SourceFailureCode.AUTH_REQUIRED
    if any(marker in lowered for marker in _RATE_MARKERS) or _RATE_STATUS.search(lowered):
        return SourceFailureCode.RATE_LIMITED
    return SourceFailureCode.TRANSIENT


class InspectSource(_Contract):
    """Identify one connected source without exposing its credentials."""

    connection_id: str = Field(min_length=1)
    root_locator: str = ""


class SourceDescription(_Contract):
    """Stable identity and synchronization capabilities of a connected source."""

    connection_id: str
    source_kind: str
    account_id: str
    data_shape: SourceDataShape = SourceDataShape.DOCUMENT
    display_name: str = ""
    supports_incremental: bool = True
    supports_deletes: bool = True
    root_locator: str = ""
    generation: int = Field(default=1, ge=1)


class SourceResource(_Contract):
    """One operator-selectable mailbox, label, folder, or source subtree."""

    resource_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    resource_kind: str = Field(min_length=1)
    locator: str = ""
    selected: bool = False
    detail: str = ""


class ListSourceResources(_Contract):
    connection_id: str = Field(min_length=1)


class SelectSourceResources(_Contract):
    connection_id: str = Field(min_length=1)
    resource_ids: tuple[str, ...] = Field(min_length=1)


class SyncSource(_Contract):
    """Request one bounded source page; ``None`` starts a new snapshot."""

    connection_id: str = Field(min_length=1)
    checkpoint: str | None = None
    root_locator: str = ""
    page_size: int = Field(default=200, ge=1, le=2_000)


class SourceObject(_Contract):
    """One versioned object or deletion tombstone from a source page."""

    object_id: str = Field(min_length=1)
    locator: str
    kind: SourceObjectKind
    version: str | None = None
    content_hash: str | None = None
    size: int | None = Field(default=None, ge=0)
    modified_at: str | None = None
    media_type: str | None = None
    deleted: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent_tombstone(self) -> SourceObject:
        if (self.kind is SourceObjectKind.DELETED) != self.deleted:
            raise ValueError(
                "deleted objects must be tombstones and only tombstones may be deleted"
            )
        return self


class SyncSourcePage(_Contract):
    """A page whose checkpoint is committed only after its objects are durable."""

    objects: tuple[SourceObject, ...] = ()
    #: ``None`` on the final page: a finished crawl commits no cursor, so the next run is
    #: a fresh full pass that can reconcile deletes.
    next_checkpoint: str | None
    has_more: bool = False


class FetchSourceObject(_Contract):
    """Fetch one exact discovered version under a strict byte ceiling."""

    connection_id: str = Field(min_length=1)
    object_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    max_bytes: int = Field(default=20_000_000, ge=1, le=100_000_000)


class SourceContent(_Contract):
    """Raw bounded bytes; extraction and indexing belong downstream."""

    object_id: str
    version: str
    media_type: str
    content: bytes
    content_hash: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class SourceAdapter(Protocol):
    """The removable contract implemented by any synchronizable connection."""

    async def inspect_source(self, request: InspectSource) -> SourceDescription: ...

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]: ...

    async def select_source_resources(self, request: SelectSourceResources) -> None: ...

    async def sync_source(self, request: SyncSource) -> SyncSourcePage: ...

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent: ...

    async def close_source(self) -> None: ...


__all__ = [
    "FetchSourceObject",
    "InspectSource",
    "ListSourceResources",
    "SelectSourceResources",
    "SourceAdapter",
    "SourceContent",
    "SourceDataShape",
    "SourceDescription",
    "SourceError",
    "SourceFailureCode",
    "SourceObject",
    "SourceObjectKind",
    "SourceResource",
    "SyncSource",
    "SyncSourcePage",
    "api_disabled_link",
    "api_disabled_name",
    "classify_cli_failure",
]
