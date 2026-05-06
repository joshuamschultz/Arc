/* ============================================================
   ArcUI Service Worker — SPEC-025 §FR-3 / SDD §C3
   Plain JS, no transpilation, no Workbox or other deps.

   Cache version: TEMPLATED by arcui/server.py at startup
   (SPEC-025 §TD-3). The literal `{{ARC_BUILD_ID}}` is replaced
   with a per-process UUID before the file is served, so every
   server restart bumps the cache key and old caches are deleted
   on the next activate. Without this, browsers serve cached
   shell forever after a deploy that changes assets.

   SECURITY: /api/*, /ws/*, /artifacts/* are network-only so
   auth-sensitive paths and live artifacts never go to disk.
   ============================================================ */

const CACHE_VERSION = 'arcui-shell-{{ARC_BUILD_ID}}';
const SHELL_PATHS = ['/', '/index.html'];
const ASSETS_PREFIX = '/assets/';
const ALWAYS_LIVE = [/^\/api\//, /^\/ws\//, /^\/artifacts\//];

// Pre-cache the bare HTML shell so a network blip does not leave
// the operator with a blank page. Each path is added independently —
// `cache.addAll` is atomic and fails the whole install if ANY request
// returns non-2xx (e.g. when the auth middleware redirects `/` for an
// unauthenticated SW fetch). Tolerant individual `put`s keep install
// succeeding; the network-first fetch handler still populates the cache
// lazily on the first authenticated navigation.
self.addEventListener('install', (event) => {
  // Take over immediately — without skipWaiting the old (broken) SW
  // keeps controlling open tabs until every one is closed, so a deploy
  // that fixes a SW bug appears to "not work" for users with sticky tabs.
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) =>
      Promise.all(
        SHELL_PATHS.map((path) =>
          fetch(path, { credentials: 'same-origin' })
            .then((resp) => {
              if (resp && resp.ok) return cache.put(path, resp);
              return undefined;  // skip non-2xx silently
            })
            .catch(() => undefined)  // skip network errors silently
        )
      )
    )
  );
});

// On activation delete every cache that is not the current version
// AND claim every existing client so the new SW handles their next
// fetch — without this the new SW only takes effect on subsequent
// page loads, leaving the broken SW in charge of the current tab.
self.addEventListener('activate', (event) => {
  event.waitUntil(
    Promise.all([
      caches.keys().then((keys) =>
        Promise.all(
          keys
            .filter((k) => k !== CACHE_VERSION)
            .map((k) => caches.delete(k))
        )
      ),
      self.clients.claim(),
    ])
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // 1. Always live (network-only — no cache touch).
  //    /api/*, /ws/*, /artifacts/* must always hit the network.
  //    Auth-sensitive paths must never go to disk.
  if (ALWAYS_LIVE.some((re) => re.test(url.pathname))) {
    return; // browser handles the request directly
  }

  // 2. Cache-first for hashed assets (/assets/*).
  //    These files have content-addressed names so a cache hit is
  //    always correct; misses are fetched, cloned, and stored.
  //    On network failure with no cache entry, return a 503 Response
  //    rather than `undefined` — respondWith(undefined) throws
  //    "Failed to convert value to 'Response'" and breaks the page.
  if (url.pathname.startsWith(ASSETS_PREFIX)) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        if (cached) return cached;
        return fetch(event.request)
          .then((response) => {
            if (response && response.ok) {
              const clone = response.clone();
              caches
                .open(CACHE_VERSION)
                .then((cache) => cache.put(event.request, clone))
                .catch(() => undefined);
            }
            return response;
          })
          .catch(() =>
            new Response('asset unavailable', {
              status: 503,
              statusText: 'Service Unavailable',
            })
          );
      })
    );
    return;
  }

  // 3. Everything else (HTML page navigation, JSON manifests, etc.) —
  //    do NOT intercept. The browser handles the request directly so
  //    auth headers from localStorage flow through unchanged and the
  //    SW never has to manufacture a Response for the navigation
  //    path. (Earlier "network-first with cache fallback" rejected
  //    promises with undefined whenever both fetch and cache missed,
  //    breaking page navigation entirely.)
  return;
});
