/* Service worker — Plan du réseau TER PACA
   - Pages et fichiers de données : réseau d'abord (toujours la dernière version), cache si hors connexion.
   - Icônes et bibliothèque de carte : cache d'abord.
   - Fonds de carte (tuiles) et autres sites : laissés au navigateur.
   Pour forcer la mise à jour chez tout le monde, change le numéro de VERSION ci-dessous. */
const VERSION = 'v30';
const STATIC = 'ter-static-' + VERSION;
const DATA = 'ter-data-' + VERSION;

const PRECACHE = [
  './',
  'index.html',
  'manifest.webmanifest',
  'favicon.png',
  'zou-logo.png',
  'favicon.ico',
  'icon-maskable-512.png',
  
  'icon-192.png',
  'icon-512.png',
  'apple-touch-icon.png'
];

const isData = (url) => /\/(horaires-paca|horaires-france|stops-france|incidents|geom-lignes)\.json$/.test(url.pathname);
const isPage = (req, url) => req.mode === 'navigate' || /\/(index\.html)?$/.test(url.pathname) || /manifest\.webmanifest$/.test(url.pathname);

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(STATIC)
      .then((c) => Promise.all(PRECACHE.map((u) => c.add(u).catch(() => {}))))   // un fichier absent ne bloque pas l'installation
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== STATIC && k !== DATA).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function networkFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const res = await fetch(req, { cache: 'no-cache' });
    if (res && res.ok) cache.put(req, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(req, { ignoreSearch: true });
    if (hit) return hit;
    if (req.mode === 'navigate') {
      const page = await caches.match('index.html');
      if (page) return page;
    }
    throw err;
  }
}

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res && (res.ok || res.type === 'opaque')) cache.put(req, res.clone());
  return res;
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  if (url.origin === self.location.origin) {
    if (isData(url)) return void e.respondWith(networkFirst(req, DATA));
    if (isPage(req, url)) return void e.respondWith(networkFirst(req, STATIC));
    return void e.respondWith(cacheFirst(req, STATIC));
  }
  // bibliothèque de carte (Leaflet) : cache d'abord ; tuiles et autres sites : réseau direct
  if (url.hostname === 'cdnjs.cloudflare.com') e.respondWith(cacheFirst(req, STATIC));
});
