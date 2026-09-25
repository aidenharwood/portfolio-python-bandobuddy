/* bandobuddy's service worker: makes the map usable where the signal isn't.
 *
 * - the page itself, its icons and Leaflet are kept, so it opens with no connection at all
 * - map tiles you've already looked at are kept (capped, and only ever what you actually viewed)
 * - places you've looked at are kept, so the nearby list and a shared link still open offline
 *
 * __VERSION__ is filled in by the server, so a new release retires the old caches.
 */
"use strict";

const VERSION = "__VERSION__";
const SHELL = `bandobuddy-shell-${VERSION}`;
const TILES = "bandobuddy-tiles";
const DATA = "bandobuddy-data";
const TILE_LIMIT = 600;          // roughly a town at a few zoom levels

const SHELL_URLS = [
  "/",
  "/manifest.webmanifest",
  "/static/icon-192.png",
  "/static/apple-touch-icon.png",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
];

const TILE_HOSTS = ["tile.openstreetmap.org", "tile.opentopomap.org", "server.arcgisonline.com",
                    "services-eu1.arcgis.com"];
// Worth keeping offline; anything else (status, search, photos, exports) is live-only.
const KEEPABLE = /^\/api\/(map|list|site\/)/;

self.addEventListener("install", event => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL);
    await Promise.allSettled(SHELL_URLS.map(url => cache.add(new Request(url, { cache: "reload" }))));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", event => {
  event.waitUntil((async () => {
    const keep = new Set([SHELL, TILES, DATA]);
    await Promise.all((await caches.keys()).filter(name => !keep.has(name)).map(name => caches.delete(name)));
    await self.clients.claim();
  })());
});

async function trim(cacheName, limit) {
  const cache = await caches.open(cacheName);
  const keys = await cache.keys();
  for (const key of keys.slice(0, Math.max(0, keys.length - limit))) await cache.delete(key);
}

async function cacheFirst(request, cacheName, limit) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request);
  if (hit) return hit;
  const response = await fetch(request);
  if (response.ok || response.type === "opaque") {
    await cache.put(request, response.clone());
    if (limit) trim(cacheName, limit);
  }
  return response;
}

async function networkFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const response = await fetch(request);
    if (response.ok) await cache.put(request, response.clone());
    return response;
  } catch (err) {
    const hit = await cache.match(request);
    if (hit) return hit;
    throw err;
  }
}

async function page(request) {
  try {
    return await fetch(request);
  } catch (err) {
    return (await caches.match("/", { cacheName: SHELL })) || Response.error();
  }
}

self.addEventListener("fetch", event => {
  const { request } = event;
  if (request.method !== "GET") return;                    // updates and settings need the server
  const url = new URL(request.url);

  if (request.mode === "navigate") {
    event.respondWith(page(request));
  } else if (TILE_HOSTS.some(host => url.hostname.endsWith(host))) {
    event.respondWith(cacheFirst(request, TILES, TILE_LIMIT));
  } else if (url.hostname === "unpkg.com") {
    event.respondWith(cacheFirst(request, SHELL));
  } else if (url.origin === self.location.origin && url.pathname.startsWith("/static/")) {
    event.respondWith(cacheFirst(request, SHELL));
  } else if (url.origin === self.location.origin && KEEPABLE.test(url.pathname)) {
    event.respondWith(networkFirst(request, DATA));
  }
});
