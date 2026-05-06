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
// the operator with a blank page.
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(SHELL_PATHS))
  );
});

// On activation delete every cache that is not the current version.
// This prevents stale asset bundles from accumulating on the device.
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== CACHE_VERSION)
          .map((k) => caches.delete(k))
      )
    )
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
  if (url.pathname.startsWith(ASSETS_PREFIX)) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        if (cached) return cached;
        return fetch(event.request).then((response) => {
          const clone = response.clone();
          caches
            .open(CACHE_VERSION)
            .then((cache) => cache.put(event.request, clone));
          return response;
        });
      })
    );
    return;
  }

  // 3. Network-first for everything else (HTML, etc.).
  //    Serves from the network when online; falls back to the cached
  //    shell when the network is unavailable so the UI rehydrates
  //    rather than showing a browser error page.
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
