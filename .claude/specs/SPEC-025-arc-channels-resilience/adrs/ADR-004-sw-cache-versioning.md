# ADR-004 — Service-worker cache key is build-id templated at serve time

**Status:** Accepted (2026-05-06)
**Spec:** SPEC-025 — Track D (service worker)
**Pillar trace:** Simplicity (one substitution point), Security (no stale code), Scalability (per-process cache namespace)

## Context

SPEC-025 §FR-3 added `packages/arcui/src/arcui/static/sw.js` to cache the
arcui static shell so a network blip doesn't leave operators with a blank
page. The initial implementation used a hardcoded `CACHE_VERSION =
'arcui-shell-v1'` constant.

Both reviewers (security M4, architecture Minor #5) flagged this as a real
footgun: every deploy that changes the static bundle requires a developer
to remember to bump the constant. Forgetting to bump means returning users
get the cached `index.html` indefinitely — no security-patch can reach
them, and operator debugging becomes "have you tried clearing your
browser cache?" forever.

The same arcui codebase already templates `{{ARC_BUILD_ID}}` in
`index.html` (`server.py` lines 175–186): a per-process `uuid.uuid4().hex[:12]`
is substituted at module-load time so every server restart bumps every
asset URL. The pattern is mature, tested, and consistent with
`index.html`'s `Cache-Control` policy.

## Decision

**`sw.js` uses the same `{{ARC_BUILD_ID}}` template substitution.**

### Implementation

1. `sw.js` declares `const CACHE_VERSION = 'arcui-shell-{{ARC_BUILD_ID}}';`
2. `arcui.server.create_app()` reads `sw.js` at module-load time, runs
   `.replace("{{ARC_BUILD_ID}}", _build_id)`, and stores the substituted
   text on `app.state.sw_js`.
3. A new route `/sw.js` handler (`_service_worker`) returns the cached
   substituted text with:
   - `Content-Type: application/javascript`
   - `Cache-Control: no-cache, no-store, must-revalidate` — the SW file
     itself never caches; the cached *content* is what manages staleness.
4. The `/sw.js` route is registered **before** the static-files mount, so
   the route handler wins over the StaticFiles fallback.

### Why not a build-time bake?

A build-time pre-process step would write `arcui-shell-<hash>` directly
into `sw.js` and commit it. Rejected because:

- There is no build step in arcui today; assets are served directly from
  source. Adding one would invert that architecture and require everyone
  to rebuild during dev.
- The serve-time substitution gives one cache key per *process*, which is
  exactly what we want for development (every restart bumps) and exactly
  what we want for production (every deploy is a new process).

### Why not just `Cache-Control: no-cache` on `sw.js`?

Browsers explicitly disregard `Cache-Control: no-cache` for service worker
files for legacy compatibility — many SW deployments relied on the implicit
24-hour SW update cycle. The `CACHE_VERSION` bump is what tells the
browser's existing SW that its caches are stale.

## Consequences

**Positive:**

- Every deploy invalidates every cache entry deterministically.
- No developer ceremony — the constant doesn't need to be bumped.
- Consistent with the existing `index.html` cache-bust pattern; one less
  template-substitution code path to maintain.
- Air-gap friendly — no external CDN, no manifest, no build pipeline.

**Negative:**

- The `{{ARC_BUILD_ID}}` token in raw `sw.js` is not a valid JS identifier
  if you read the file off disk and try to execute it directly. Mitigation:
  static tests reference the **template form**, not a fully-substituted
  version, and the route-level test exercises the substitution path.
- One Python route handler the static mount can't replace — minor.

## Verification

- `tests/unit/test_sw_static.py::test_sw_has_templated_cache_name` — pins
  the template token in the source file
- `tests/unit/test_sw_static.py::test_sw_route_substitutes_build_id` —
  pins that the served bytes have the token replaced with a hex build ID
  and never contain the literal `{{ARC_BUILD_ID}}`

## Code anchors

- `packages/arcui/src/arcui/static/sw.js` line ~16 (`const CACHE_VERSION = ...`)
- `packages/arcui/src/arcui/server.py`:
  - line ~180–195 (`cached_sw_js` build at startup)
  - `_service_worker` route handler (~line 88)
  - `Route("/sw.js", _service_worker)` at the routes-list head
  - `app.state.sw_js` stash
