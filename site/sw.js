// Now · service worker
// - La app (HTML, iconos) se sirve desde caché y se actualiza en segundo plano.
// - Las noticias (data/*.json) se piden siempre a la red; si no hay conexión
//   se muestra la última versión guardada.
const SHELL_CACHE = "now-shell-v3";
const DATA_CACHE = "now-data-v3";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon.svg", "./icon-192.png", "./apple-touch-icon.png"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(SHELL_CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => ![SHELL_CACHE, DATA_CACHE].includes(k)).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // Noticias: red primero, caché si no hay conexión
  if (url.origin === location.origin && url.pathname.includes("/data/")) {
    const key = url.origin + url.pathname;          // sin el ?t= anti-caché
    event.respondWith(
      fetch(req).then(res => {
        if (res.ok) { const copy = res.clone(); caches.open(DATA_CACHE).then(c => c.put(key, copy)); }
        return res;
      }).catch(() => caches.match(key).then(hit => hit || Response.error()))
    );
    return;
  }

  // App y tipografías: caché primero, actualización en segundo plano
  if (url.origin === location.origin || url.hostname.endsWith("fonts.googleapis.com") || url.hostname.endsWith("fonts.gstatic.com")) {
    event.respondWith(
      caches.open(SHELL_CACHE).then(cache => cache.match(req).then(hit => {
        const net = fetch(req).then(res => { if (res.ok || res.type === "opaque") cache.put(req, res.clone()); return res; }).catch(() => hit);
        return hit || net;
      }))
    );
  }
});

// ─── Notificaciones push ────────────────────────────────────────────────────
// El servidor (build.py, vía GitHub Actions) envía {title, body, url, tag}.
self.addEventListener("push", event => {
  let m = {};
  try { m = event.data ? event.data.json() : {}; } catch (e) { m = { body: event.data ? event.data.text() : "" }; }
  event.waitUntil(self.registration.showNotification(m.title || "Now", {
    body: m.body || "", tag: m.tag || "now", icon: "icon-192.png", badge: "icon-192.png",
    data: { url: m.url || "./" }, renotify: false,
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || "./", self.registration.scope).href;
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    for (const c of list) {
      if (c.url.startsWith(self.registration.scope)) {
        c.postMessage({ type: "open", hash: new URL(target).hash });
        return c.focus();
      }
    }
    return self.clients.openWindow(target);
  }));
});
