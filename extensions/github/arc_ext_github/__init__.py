"""GitHub CLI-backed connected document source."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceContent,
    SourceDataShape,
    SourceDescription,
    SourceError,
    SourceFailureCode,
    SourceObject,
    SourceObjectKind,
    SourceResource,
    SyncSource,
    SyncSourcePage,
)

#: Extensions worth indexing as text. A repository is mostly code and prose;
#: everything else — images, archives, compiled output, lockfiles — is bytes
#: that no extractor can read and that would only crowd out real results.
_TEXT_SUFFIXES: frozenset[str] = frozenset(
    {
        ".md",
        ".markdown",
        ".rst",
        ".txt",
        ".adoc",
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".rb",
        ".java",
        ".kt",
        ".swift",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".php",
        ".scala",
        ".sh",
        ".bash",
        ".zsh",
        ".sql",
        ".r",
        ".jl",
        ".lua",
        ".pl",
        ".ex",
        ".exs",
        ".html",
        ".css",
        ".scss",
        ".vue",
        ".svelte",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".env.example",
    }
)

#: One file this large is not documentation or code anyone searches for; it is a
#: vendored bundle or generated output, and indexing it buries the rest.
_MAX_FILE_BYTES = 512 * 1024


class GitHubSourceAdapter:
    """Index selected repositories: their issues, pull requests, and files."""

    def __init__(self, attachment: Any) -> None:
        self._attachment = attachment
        self._repos: tuple[str, ...] = ()
        self._content: dict[str, tuple[str, bytes]] = {}

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        repos = await self._call("github_repo_list", {"limit": "1"})
        account = (
            str(repos[0].get("nameWithOwner", "github")).split("/", 1)[0] if repos else "github"
        )
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="github",
            account_id=account,
            data_shape=SourceDataShape.DOCUMENT,
            display_name=f"GitHub ({account})",
            supports_incremental=False,
            supports_deletes=False,
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        del request
        repos = await self._call_all("github_repo_list", {})
        return tuple(
            SourceResource(
                resource_id=str(repo["nameWithOwner"]),
                label=str(repo["nameWithOwner"]),
                resource_kind="repository",
                locator=str(repo["nameWithOwner"]),
            )
            for repo in repos
            if repo.get("nameWithOwner")
        )

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        resources = await self.list_source_resources(
            ListSourceResources(connection_id=request.connection_id)
        )
        allowed = {resource.resource_id for resource in resources}
        if not set(request.resource_ids).issubset(allowed):
            raise ValueError("invalid GitHub repository selection")
        self._repos = request.resource_ids

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        resources = await self.list_source_resources(
            ListSourceResources(connection_id=request.connection_id)
        )
        repos = self._repos or tuple(resource.resource_id for resource in resources)
        start = int(request.checkpoint or "0")
        records: list[tuple[str, str, dict[str, Any]]] = []
        for repo in repos:
            for kind, tool in (("issue", "github_issue_list"), ("pull", "github_pr_list")):
                args = {"repo": repo, "state": "all", "limit": "1000"}
                for item in await self._optional_collection(tool, args):
                    records.append((repo, kind, item))
            # The repository itself, not only the conversation around it. Issues
            # and pull requests are what people said; the files are the thing
            # they were talking about, and indexing one without the other left a
            # code search that could not find any code.
            for entry in await self._tree(repo):
                records.append((repo, "file", entry))
        page_records = records[start : start + request.page_size]
        objects = tuple(self._to_object(repo, kind, item) for repo, kind, item in page_records)
        next_index = start + len(page_records)
        return SyncSourcePage(
            objects=objects,
            next_checkpoint=str(next_index) if next_index < len(records) else "0",
            has_more=next_index < len(records),
        )

    async def _tree(self, repo: str) -> list[dict[str, Any]]:
        """Every indexable file in one repository, at its default branch.

        A tree read is one call for the whole repository. GitHub truncates a
        very large one and says so; a truncated tree is indexed as far as it
        goes rather than refused, because a partial repository is still worth
        searching and the alternative is none of it.
        """
        result = await self._attachment.invoke("github_repo_tree", {"repo": repo})
        if str(result.outcome) != "ok":
            detail = str(result.content)[:256]
            if _repository_simply_lacks_it(detail):
                # A repository with no commits yet has no tree, and gh says so
                # with a 409. Nothing to index is not a failure to index.
                return []
            raise SourceError(SourceFailureCode.TRANSIENT, detail)
        payload = json.loads(result.content)
        entries = payload.get("tree", []) if isinstance(payload, dict) else []
        return [entry for entry in entries if _is_indexable(entry)]

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        if ":file:" in request.object_id:
            return await self._fetch_file(request)
        version, content = self._content[request.object_id]
        if version != request.version:
            raise ValueError("GitHub object version changed")
        if len(content) > request.max_bytes:
            raise SourceError(SourceFailureCode.TOO_LARGE, "GitHub object exceeds byte limit")
        return SourceContent(
            object_id=request.object_id,
            version=version,
            media_type="application/json",
            content=content,
        )

    async def _fetch_file(self, request: FetchSourceObject) -> SourceContent:
        """Read one file's bytes by the exact blob that was listed.

        The blob, not the path at a ref: a branch moves while a crawl is
        running, and indexing one revision's text under another's version is how
        a search starts quoting a line that is no longer there. A blob sha is
        also not a valid ``ref`` — the contents endpoint answers 404 for one —
        so the blobs endpoint is both the correct read and the only one that
        keeps the pin.
        """
        repo, _, _path = request.object_id.partition(":file:")
        result = await self._attachment.invoke(
            "github_file_content",
            {"blob": f"{repo}/git/blobs/{request.version}"},
        )
        if str(result.outcome) != "ok":
            if _is_empty_blob(result):
                # `gh` exits 0 and writes nothing for an empty file, and the
                # attachment reads "no output" as a failure. A repository is full
                # of legitimately empty files — `__init__.py`, `.gitkeep` — and
                # one of them ended the entire crawl, because the coordinator
                # skips only TOO_LARGE and re-raises every other refusal.
                return SourceContent(
                    object_id=request.object_id,
                    version=request.version,
                    media_type="text/plain",
                    content=b"",
                )
            raise SourceError(SourceFailureCode.NOT_FOUND, str(result.content)[:256])
        content = str(result.content).encode()
        if len(content) > request.max_bytes:
            raise SourceError(SourceFailureCode.TOO_LARGE, "GitHub file exceeds byte limit")
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            # Empty on purpose: the extractor resolves by the file's own
            # extension, which is the only thing that knows a .py from a .md.
            media_type="",
            content=content,
        )

    async def close_source(self) -> None:
        self._content.clear()

    async def _call(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        result = await self._attachment.invoke(tool, args)
        if str(result.outcome) != "ok":
            detail = str(result.content)
            raise SourceError(_github_failure_code(detail), detail[:256])
        parsed = json.loads(result.content)
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]

    async def _optional_collection(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        """A collection a repository is allowed not to have.

        Issues and pull requests can be switched off per repository, and ``gh``
        answers a request for one that is off with an error rather than an empty
        list. That is a normal state of a normal repository, not a sync failure:
        letting it escape aborted the whole crawl on the first such repo, so five
        others and every file in them went unindexed.

        Only "this repository does not have that" is absorbed — see
        :func:`_repository_simply_lacks_it`. Auth failures, rate limits and
        truncated pages still refuse.
        """
        try:
            return await self._call_all(tool, args)
        except SourceError as exc:
            if not _repository_simply_lacks_it(exc.detail):
                raise
            return []

    async def _call_all(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        """Read a complete CLI collection, refusing a provider-imposed hard cap.

        ``gh`` exposes a limit but no offset. Increasing the requested limit is its
        supported pagination mechanism; a constant response after increasing the
        limit means the provider capped the result and must not be treated as a
        complete collection.
        """
        requested = 200
        previous = -1
        while requested <= 100_000:
            payload = await self._call(tool, {**args, "limit": str(requested)})
            count = len(payload)
            if count < requested:
                return payload
            if count <= previous:
                raise SourceError(
                    SourceFailureCode.TRANSIENT,
                    f"GitHub returned a bounded page for {tool}; refusing a partial index",
                )
            previous = count
            requested *= 2
        raise SourceError(
            SourceFailureCode.TOO_LARGE,
            f"GitHub {tool} collection exceeds the safe synchronization bound",
        )

    def _to_object(self, repo: str, kind: str, item: dict[str, Any]) -> SourceObject:
        if kind == "file":
            return self._file_object(repo, item)
        return self._object(repo, kind, item)

    def _file_object(self, repo: str, entry: dict[str, Any]) -> SourceObject:
        """One repository file, versioned by its blob sha.

        The sha IS the content, so it is the honest version: a file that has not
        changed keeps its version across crawls and is never re-indexed, and one
        that has changed cannot keep the old one.
        """
        path = str(entry.get("path") or "")
        sha = str(entry.get("sha") or "")
        return SourceObject(
            object_id=f"{repo}:file:{path}",
            locator=path,
            kind=SourceObjectKind.FILE,
            version=sha,
            content_hash=sha,
            size=int(entry.get("size") or 0),
            media_type="",
            metadata={
                "repository": repo,
                "kind": "file",
                "path": path,
                "classification": "unclassified",
                "revision": _sha_revision(sha),
            },
        )

    def _object(self, repo: str, kind: str, item: dict[str, Any]) -> SourceObject:
        number = str(item.get("number", ""))
        object_id = f"{repo}:{kind}:{number}"
        content = json.dumps(
            {"repository": repo, "kind": kind, **item}, ensure_ascii=False
        ).encode()
        version = str(item.get("updatedAt") or hashlib.sha256(content).hexdigest())
        self._content[object_id] = (version, content)
        return SourceObject(
            object_id=object_id,
            locator=str(item.get("url") or object_id),
            kind=SourceObjectKind.FILE,
            version=version,
            content_hash=hashlib.sha256(content).hexdigest(),
            size=len(content),
            modified_at=str(item.get("updatedAt") or "") or None,
            media_type="application/json",
            metadata={
                "repository": repo,
                "kind": kind,
                "classification": "unclassified",
                "revision": _timestamp_revision(item.get("updatedAt")),
            },
        )


def build_source_adapter(context: dict[str, Any]) -> GitHubSourceAdapter:
    """Build from the already policy-bound CLI attachment."""
    return GitHubSourceAdapter(context["attachment"])


#: What ``gh`` says when a repository simply does not have the thing asked for:
#: a feature switched off, or a repository with no commits yet. Both are normal
#: states of a normal repository, and neither is a reason to abandon the crawl.
_ABSENT_MARKERS = ("has disabled", "repository is empty")


def _is_empty_blob(result: Any) -> bool:
    """True when the CLI succeeded and simply had no bytes to write.

    Keyed on the exit status the message reports, not on "the content is empty":
    a failure that happens to print nothing must still be a failure.
    """
    return "exited 0" in str(result.content)


#: Rate limiting, which means "wait", not "reconnect".
_RATE_MARKERS = ("rate limit", "secondary rate", "429")

#: A credential no retry will fix.
_AUTH_MARKERS = ("bad credentials", "401", "requires authentication", "must be logged in")


def _github_failure_code(detail: str) -> SourceFailureCode:
    """Classify a `gh` failure so the coordinator can act on it.

    Everything used to be an untyped ``RuntimeError``, which the coordinator
    treats as a hard failure — so one TLS handshake timeout, a single call into a
    crawl of every repository in an account, threw the whole run away. A crawl
    that long WILL hit one.

    Anything not clearly a credential or a rate limit is TRANSIENT, which is a
    deliberate default rather than a list of network phrasings to keep current:
    the coordinator retries a bounded number of times and then fails, so a truly
    permanent error still ends the run — a few seconds later, having cost some
    retries. Losing a whole account's crawl to a blip is the worse trade.
    """
    lowered = detail.lower()
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return SourceFailureCode.AUTH_REQUIRED
    if any(marker in lowered for marker in _RATE_MARKERS):
        return SourceFailureCode.RATE_LIMITED
    return SourceFailureCode.TRANSIENT


def _repository_simply_lacks_it(detail: str) -> bool:
    """True when a refusal means "there is none", not "something went wrong"."""
    lowered = detail.lower()
    return any(marker in lowered for marker in _ABSENT_MARKERS)


def _is_indexable(entry: object) -> bool:
    """A text file small enough to be worth reading."""
    if not isinstance(entry, dict) or entry.get("type") != "blob":
        return False
    path = str(entry.get("path") or "")
    if not path:
        return False
    size = entry.get("size")
    if isinstance(size, int) and size > _MAX_FILE_BYTES:
        return False
    suffix = path[path.rfind(".") :].lower() if "." in path else ""
    return suffix in _TEXT_SUFFIXES


def _sha_revision(sha: str) -> int:
    """A monotonic-ish revision from a content sha.

    A blob has no timestamp, and the ingest side needs an integer it can compare
    to decide whether an update is newer. The sha is stable per content, so the
    same file yields the same revision and a changed one yields a different
    value — which is exactly the question being asked.
    """
    return int(sha[:12], 16) if sha else 1


def _timestamp_revision(value: object) -> int:
    if not isinstance(value, str) or not value:
        return 1
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000)


__all__ = ["GitHubSourceAdapter", "build_source_adapter"]
