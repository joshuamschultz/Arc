// ArcUI kill-switch service worker.
//
// The previous ArcUI shipped a caching service worker that cached the app
// shell. After the React rebuild, any browser that still has the old SW
// installed would keep serving the stale vanilla shell. This replacement
// SW does the opposite of caching: it unregisters itself and purges every
// cache, then never intercepts a fetch again. Vite content-hashes all
// assets, so no service worker is needed for cache-busting going forward.
self.addEventListener('install', () => {
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys()
      await Promise.all(keys.map((k) => caches.delete(k)))
      await self.registration.unregister()
      const clients = await self.clients.matchAll({ type: 'window' })
      for (const client of clients) {
        client.navigate(client.url)
      }
    })(),
  )
})
