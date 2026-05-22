const CACHE = 'ta-v11';
// Only cache true static assets — NOT index.html.
// HTML changes on every deploy; caching it causes stale JS to run after updates.
const STATIC = ['/manifest.json', '/icon-192.svg', '/icon-512.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  if (e.request.url.includes('/api/') || e.request.url.startsWith('ws')) return;
  // Always fetch HTML fresh from network — never serve stale index.html from cache.
  if (e.request.mode === 'navigate') return;
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
