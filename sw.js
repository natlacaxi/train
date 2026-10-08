/* Service worker : permet d'installer le site et de l'ouvrir hors connexion.
   - pages et fichiers .json (horaires, incidents) : réseau d'abord, copie en cache si hors ligne
   - icônes et autres fichiers : cache d'abord */
const V = "ter-v1";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./favicon.png", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(V).then(c => Promise.all(SHELL.map(u => c.add(u).catch(() => {})))).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const r = e.request;
  if (r.method !== "GET") return;
  const u = new URL(r.url);
  if (u.origin !== location.origin) return;           // API externes : pas de cache
  const store = res => { if (res && res.ok) { const cp = res.clone(); caches.open(V).then(c => c.put(r, cp)); } return res; };
  const network = r.mode === "navigate" || /\.(json|html)$/.test(u.pathname) || u.pathname.endsWith("/");
  if (network) {
    e.respondWith(fetch(r).then(store).catch(() => caches.match(r).then(m => m || caches.match("./index.html"))));
  } else {
    e.respondWith(caches.match(r).then(m => m || fetch(r).then(store)));
  }
});
