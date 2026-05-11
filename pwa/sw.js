/* Service Worker — XAGUSD Analyser PWA */
const CACHE = "xagusd-v6";
const SHELL = ["/", "/index.html", "/manifest.json", "/icon-192.svg"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", e => {
  if (e.request.url.includes("/api/")) return; // always network for API
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});

/* Push notification handler */
self.addEventListener("push", e => {
  let data = { title: "XAGUSD Update", body: "New analysis available" };
  try { data = e.data.json(); } catch {}
  e.waitUntil(
    self.registration.showNotification(data.title, {
      body:    data.body,
      icon:    "/icon-192.svg",
      badge:   "/icon-192.svg",
      vibrate: [200, 100, 200],
      data:    { url: "/" },
      actions: [{ action: "open", title: "View Analysis" }],
    })
  );
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(clients.openWindow(e.notification.data?.url ?? "/"));
});
