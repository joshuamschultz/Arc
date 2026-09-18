# Connector authoring contract

A connector attaches an external system to an agent by implementing the
`SourceAdapter` seam (`arcagent.extension.source.SourceAdapter`). The seam has an
unwritten contract every adapter must honour, so the eleventh connector inherits
the reliability fixes the first ten earned instead of re-earning the same bugs.

The contract is codified as a runnable checker:
`arcagent.extension.authoring.assert_connector_contract`. Write a test that runs
it against your adapter (offline, with no live backend) and it will fail the
build if the adapter breaks any clause.

```python
from arcagent.extension.authoring import assert_connector_contract

async def test_my_connector_honours_the_contract() -> None:
    await assert_connector_contract(MyAdapter(), connection_id="test-conn")
```

## The four clauses

### 1. Monotonic revision

Every object a sync returns carries a **non-decreasing** `metadata["revision"]`.

ArcMemory skips an unchanged object by its content `version` and only consults the
revision when the content changed — where the new revision must be strictly
greater than the stored one. A source that emits a **decreasing** revision
therefore silently stops re-indexing: the newer object is dropped as stale.

If your source has no natural monotonic field, synthesise one. `arc_ext_dropbox`
derives `revision` from `server_modified` (`_modified_revision`); `jira` uses the
issue `updated` timestamp, falling back to the current time so a later crawl
always carries a higher revision (`_revision_for`).

```python
SourceObject(
    object_id=key,
    locator=locator,
    kind=SourceObjectKind.FILE,
    metadata={"revision": monotonic_revision},   # required, non-decreasing
)
```

### 2. Lazy client re-creation after `close_source`

After `close_source()`, a later call must **still work** — the adapter rebuilds
its client rather than holding a dead handle.

`close_source` releases connections and caches at the end of a run. The next run
reuses the same adapter instance, so any method that needs a client must rebuild
it lazily if it was torn down:

```python
async def sync_source(self, request: SyncSource) -> SyncSourcePage:
    if self._client is None:      # rebuilt after close_source
        self._client = self._build_client()
    ...

async def close_source(self) -> None:
    self._client = None
```

### 3. Typed failures

A refusal surfaces as a **typed, coded** error — never a bare `Exception` an
orchestrator cannot classify or back off from.

Raise `SourceError(SourceFailureCode, detail, retry_after=...)`. The failure code
lets the orchestrator act without knowing the vendor: `AUTH_REQUIRED` triggers
credential renewal, `RATE_LIMITED` (with `retry_after`) backs off, `TOO_LARGE` and
`NOT_FOUND` are terminal for one object, and so on.

```python
raise SourceError(SourceFailureCode.AUTH_REQUIRED, "the connection needs re-authorising")
```

Errors that leak up from the attachment layer as a typed `ArcAgentError` (for
example an `ExtensionError` when a vendor binary is missing) are tolerated as
typed today, but the connector is stronger if it catches them at the boundary and
re-raises the appropriate `SourceError` code.

### 4. Tool refusals are returned, not raised

At the `ExtensionAttachment` seam (`invoke`), a tool-level refusal is returned as
`ToolResult(outcome=ToolOutcome.ERROR)` with the failure text in `content` — it is
**not** raised. A raised exception at that seam means the wire or protocol failed;
an in-band `ERROR` result means the tool ran and reported an actionable answer the
agent can read and self-correct from. Keep the two tiers distinct.

```python
return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content=detail)
```

## Why the checker passes a real bundle offline

`assert_connector_contract` is runnable with no live backend. A bundle like
`jira` reaches its backend through a vendor CLI that is absent in CI, so its
lifecycle raises only typed errors — and the checker treats a typed failure as
conformant, because the clause governs *how* a failure surfaces, not that a
fresh, un-credentialed connection must succeed. A fake that raises a bare
`RuntimeError`, returns a decreasing revision, or holds a dead client after
`close_source` is flagged.
