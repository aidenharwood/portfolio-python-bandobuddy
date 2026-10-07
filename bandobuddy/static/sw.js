/* bandobuddy's service worker: makes the map usable where the signal isn't.
 *
 * - the page itself, its icons and Leaflet are kept, so it opens with no connection at all
 * - every map tile you've looked at is kept (tens of thousands of them: only ever what you actually
 *   viewed, which is what OpenStreetMap's tile policy allows)
 * - every place in brief, and the full details of places in the areas you've looked around, are kept
 *   in localdb.js; the map, the list, search and a place's page are answered from there, the same way
 *   the server would answer them: first, whenever that copy matches the server's map (it's quicker than
 *   asking), and with no signal (or a very weak one) whatever its age
 * - the app itself opens from its kept copy, and checks for a newer one behind the scenes
 * - "Getting there", street photos and town or postcode searches are kept too, once you've used them
 *
 * __VERSION__ is filled in by the server, so a new release retires the old caches.
 */
"use strict";

const VERSION = "__VERSION__";
importScripts(`/static/localdb.js?v=${VERSION}`);

const SHELL = `bandobuddy-shell-${VERSION}`;
const TILES = "bandobuddy-tiles";
const DATA = "bandobuddy-data";
const PHOTOS = "bandobuddy-photos";
const TILE_LIMIT = 40000;        // every tile you've scrolled past: about 1 GB of map at most
const DATA_LIMIT = 3000;         // views, paths and photo lists: a few MB at most
const PHOTO_LIMIT = 1500;        // street photos you've looked at: small thumbnails, 25 MB or so
const TRIM_CHANCE = 1 / 250;     // counting a big cache is slow: tidy up now and then, not on every tile
const FULL = 0.85;               // share of the browser's storage allowance before old tiles make room
const SLOW_MS = 6000;            // a weak signal: after this long, answer from the phone instead

// The app can't open without these, so a new version only takes over once it has all of them: if an
// update half-downloads on a weak signal, the old version (and its complete copy) stays in charge.
const ESSENTIAL = [
  "/",
  `/static/localdb.js?v=${VERSION}`,
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
];
const EXTRAS = ["/manifest.webmanifest", "/static/icon-192.png", "/static/apple-touch-icon.png"];

const TILE_HOSTS = ["tile.openstreetmap.org", "tile.opentopomap.org", "server.arcgisonline.com",
                    "services-eu1.arcgis.com",                                    // coal mining overlays
                    "environment.data.gov.uk", "ows.remotesensing.data.gov.scot"];   // LiDAR, England and Scotland

self.addEventListener("install", event => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL);
    await Promise.all(ESSENTIAL.map(url => cache.add(new Request(url, { cache: "reload" }))));  // or don't install
    await Promise.allSettled(EXTRAS.map(url => cache.add(new Request(url, { cache: "reload" }))));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", event => {
  event.waitUntil((async () => {
    const keep = new Set([SHELL, TILES, DATA, PHOTOS]);
    await Promise.all((await caches.keys()).filter(name => !keep.has(name)).map(name => caches.delete(name)));
    await dropOpaqueTiles();
    await self.clients.claim();
  })());
});

// Tiles kept before 0.9 were fetched without CORS, and browsers count each of those as about 7 MB of
// storage: a few hundred would look like gigabytes and crowd out everything else. Drop them once;
// they're kept again, properly, as you look at the map.
async function dropOpaqueTiles() {
  try {
    if (!(await LocalDB.once("tiles-with-cors"))) return;
    const cache = await caches.open(TILES);
    for (const key of await cache.keys()) {
      const kept = await cache.match(key);
      if (kept && kept.type === "opaque") await cache.delete(key);
    }
  } catch (_) { /* storage blocked: nothing kept to tidy */ }
}

// Oldest first, down to the limit; further if the browser's storage allowance is nearly used up.
async function trim(cacheName, limit) {
  const cache = await caches.open(cacheName);
  const keys = await cache.keys();
  let excess = keys.length - limit;
  try {
    const { usage, quota } = await navigator.storage.estimate();
    if (quota && usage / quota > FULL) excess = Math.max(excess, Math.ceil(keys.length / 10));
  } catch (_) { /* no estimate: the limit will do */ }
  for (const key of keys.slice(0, Math.max(0, excess))) await cache.delete(key);
}

async function keep(cache, cacheName, limit, request, response) {
  try {
    await cache.put(request, response);
  } catch (err) {   // full: make room, and do without this one
    if (limit) await trim(cacheName, Math.floor(limit * 0.9));
    return;
  }
  if (limit && Math.random() < TRIM_CHANCE) trim(cacheName, limit);
}

async function cacheFirst(request, cacheName, limit) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request);
  if (hit) return hit;
  const response = await fetch(request);
  if (response.ok || response.type === "opaque") await keep(cache, cacheName, limit, request, response.clone());
  return response;
}

function centreOf(url) {
  const bbox = new URL(url).searchParams.get("bbox");
  if (!bbox) return null;
  const [w, s, e, n] = bbox.split(",").map(Number);
  return [w, s, e, n].some(Number.isNaN) ? null : [(w + e) / 2, (s + n) / 2];
}

/* Offline, nothing kept on the phone yet, and this exact view was never loaded? Answer with the nearest
 * one that was, rather than nothing at all. The app says the pins are from somewhere else. */
async function nearestKept(cache, request) {
  const wanted = centreOf(request.url);
  if (!wanted) return null;
  const path = new URL(request.url).pathname;
  let best = null;
  let closest = Infinity;
  for (const key of await cache.keys()) {
    if (new URL(key.url).pathname !== path) continue;
    const centre = centreOf(key.url);
    if (!centre) continue;
    const away = (centre[0] - wanted[0]) ** 2 + (centre[1] - wanted[1]) ** 2;
    if (away < closest) {
      closest = away;
      best = key;
    }
  }
  return best ? cache.match(best) : null;
}

async function markStale(response) {
  const headers = new Headers(response.headers);
  headers.set("X-Bandobuddy-Stale", "1");
  headers.delete("Content-Encoding");
  headers.delete("Content-Length");
  return new Response(await response.blob(), { status: response.status, headers });
}

// Give up waiting for a weak signal, but only when there's something kept to answer with instead.
function patience(promise, ms) {
  if (!ms) return promise;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("slow")), ms);
    promise.then(r => { clearTimeout(timer); resolve(r); }, e => { clearTimeout(timer); reject(e); });
  });
}

async function fromPhone(url) {
  let data = null;
  try { data = await LocalDB.respond(url); } catch (_) { /* storage blocked or cleared */ }
  return data && new Response(JSON.stringify(data), {
    status: 200, headers: { "Content-Type": "application/json", "X-Bandobuddy-Local": "1" } });
}

async function haveIndex() {
  try { return !!(await LocalDB.index()); } catch (_) { return false; }
}

async function copyIsCurrent() {
  try { return await LocalDB.current(); } catch (_) { return false; }
}

// Answered from the phone, and it's as new as the server's: say so, so the page treats it as the real thing.
async function fromPhoneFirst(url) {
  const answer = await fromPhone(url);
  if (!answer) return null;
  const headers = new Headers(answer.headers);
  headers.set("X-Bandobuddy-Local", "current");
  return new Response(answer.body, { status: 200, headers });
}

// The map and the list: the copy on the phone when it's the server's map as it stands (quicker than asking);
// otherwise the server when it answers, and the copy on the phone when it doesn't.
async function view(request) {
  if (await copyIsCurrent()) {
    const answer = await fromPhoneFirst(request.url);
    if (answer) return answer;
  }
  const local = await haveIndex();
  const cache = await caches.open(DATA);
  try {
    const response = await patience(fetch(request), local ? SLOW_MS : 0);
    if (response.status >= 500 && local) throw new Error(`server ${response.status}`);
    if (response.ok && !local) await keep(cache, DATA, DATA_LIMIT, request, response.clone());
    return response;
  } catch (err) {
    const answer = local && await fromPhone(request.url);
    if (answer) return answer;
    // Nothing kept on the phone yet (the first minutes after installing): the views you've loaded.
    const hit = await cache.match(request);
    if (hit) return hit;
    const nearby = await nearestKept(cache, request);
    if (nearby) return markStale(nearby);
    return offlineAnswer();
  }
}

// One place: what the phone kept, straight away, while the server's newer word is fetched and kept for next
// time; or the server, when nothing's kept (and keep what it says); otherwise what the phone knows.
async function place(request, event) {
  const kept = LocalDB.detail(decodeURIComponent(new URL(request.url).pathname.slice("/api/site/".length)))
    .catch(() => null);
  if (await kept && navigator.onLine) {
    event.waitUntil(fetch(request).then(r => (r.ok ? r.json() : null))
      .then(site => site && LocalDB.putDetails([site])).catch(() => {}));
    const answer = await fromPhoneFirst(request.url);
    if (answer) return answer;
  }
  try {
    const response = await patience(fetch(request), (await kept) ? SLOW_MS : 0);
    if (response.status >= 500 && await kept) throw new Error(`server ${response.status}`);
    if (response.ok) response.clone().json().then(site => LocalDB.putDetails([site])).catch(() => {});
    return response;
  } catch (err) {
    const answer = await fromPhone(request.url);
    if (answer) return answer;
    const hit = await (await caches.open(DATA)).match(request);   // kept by an older version
    return hit || offlineAnswer();
  }
}

// Street photos: fetched with CORS where the host allows it (Panoramax does), so they're kept at their
// real size. One that doesn't is still shown, just not kept.
async function photo(request) {
  const cache = await caches.open(PHOTOS);
  const hit = await cache.match(request.url);
  if (hit) return hit;
  let response;
  try {
    response = await fetch(request.url, { mode: "cors", credentials: "omit" });
  } catch (err) {
    return fetch(request);
  }
  if (response.ok) await keep(cache, PHOTOS, PHOTO_LIMIT, request.url, response.clone());
  return response;
}

// Kept answers straight away, refreshed behind the scenes for next time ("Getting there", photo lists).
async function keptFirst(request, cacheName, event) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request);
  const fresh = fetch(request).then(async response => {
    if (response.ok) await keep(cache, cacheName, DATA_LIMIT, request, response.clone());
    return response;
  });
  if (hit) {
    event.waitUntil(fresh.catch(() => {}));
    return hit;
  }
  return fresh.catch(() => offlineAnswer());
}

async function networkFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const response = await fetch(request);
    if (response.ok) await keep(cache, cacheName, DATA_LIMIT, request, response.clone());
    return response;
  } catch (err) {
    return (await cache.match(request)) || offlineAnswer();
  }
}

const OFFLINE_PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>bandobuddy</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;font:16px system-ui,sans-serif;
background:#1a1916;color:#ece8e1;text-align:center;padding:24px}button{margin-top:16px;padding:10px 18px;
border:0;border-radius:10px;background:#fb923c;color:#1a1206;font:inherit;font-weight:700}</style></head>
<body><div><h1>bandobuddy</h1><p>You're offline, and this phone doesn't have the app saved yet.<br>
Open it once with a signal and it'll work offline after that.</p>
<button onclick="location.reload()">Try again</button></div></body></html>`;

async function tell(message) {
  for (const client of await self.clients.matchAll({ type: "window" })) client.postMessage(message);
}

async function page(request, event) {
  const cache = await caches.open(SHELL);
  const home = new URL(request.url).pathname === "/";
  // Keep the freshest copy of the app itself, so it opens offline as you last saw it.
  const fresh = fetch(request).then(async response => {
    if (response.ok && home) await cache.put("/", response.clone());
    return response;
  });
  if (home) {   // open the kept copy straight away; the newer one fetched now is used next time
    const kept = await cache.match("/");
    if (kept) {
      const opened = kept.clone().text();
      event.waitUntil(fresh.then(async response => {
        // Not the app that just opened: say so, so it can be reloaded now rather than found out next time.
        if (response.ok && (await response.clone().text()) !== (await opened)) await tell({ type: "page-updated" });
      }).catch(() => {}));
      return kept;
    }
  }
  try {
    return await fresh;
  } catch (err) {
    return (await cache.match("/")) || (await caches.match("/"))
      || new Response(OFFLINE_PAGE, { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } });
  }
}

// Asked for something that wasn't kept, with no signal: say so plainly, as the app's own error.
function offlineAnswer() {
  return new Response(JSON.stringify({ error: "You're offline, and this wasn't kept on your phone.", offline: true }),
                      { status: 503, headers: { "Content-Type": "application/json", "X-Bandobuddy-Offline": "1" } });
}

// News of saved places, in the background: where the browser offers it (Chrome on Android, once the app's
// installed), it wakes this now and then. The copy of the map is brought up to date, then each saved place is
// compared with what a notification last said about it. Nothing about your saved places leaves the phone.
self.addEventListener("periodicsync", event => {
  if (event.tag === "saved-places") event.waitUntil(tellSavedNews());
});

async function tellSavedNews() {
  const watch = await LocalDB.watchList();
  if (!watch.notify || !watch.places.length) return;
  try {
    const kept = await LocalDB.index();
    const res = await fetch(`/api/index?${new URLSearchParams({ have: kept ? kept.built : "" })}`);
    if (res.ok) {
      const data = await res.json();
      if (data.unchanged) await LocalDB.touchIndex();
      else await LocalDB.putIndex(data);
    }
  } catch (_) { /* no signal: what's kept will do */ }
  const news = await LocalDB.savedNews(watch, "told");
  for (const n of news) {
    await self.registration.showNotification(n.name, { body: n.said.join(" · "), tag: `saved:${n.key}`,
                                                       data: { key: n.key }, icon: "/static/icon-192.png" });
  }
  if (news.length) {
    const told = Object.fromEntries(news.map(n => [n.key, n.now]));
    await LocalDB.putWatch({ ...watch, places: watch.places.map(p => (told[p.key] ? { ...p, told: told[p.key] } : p)) });
  }
}

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const key = event.notification.data && event.notification.data.key;
  const url = key ? `/#site=${encodeURIComponent(key)}` : "/";
  event.waitUntil((async () => {
    for (const client of await self.clients.matchAll({ type: "window", includeUncontrolled: true })) {
      if ("focus" in client) {
        try { await client.navigate(url); } catch (_) { /* another page of ours: focusing it will do */ }
        return client.focus();
      }
    }
    return self.clients.openWindow(url);
  })());
});

self.addEventListener("fetch", event => {
  const { request } = event;
  if (request.method !== "GET") return;                    // updates and settings need the server
  const url = new URL(request.url);
  const ours = url.origin === self.location.origin;

  if (request.mode === "navigate") {
    event.respondWith(page(request, event));
  } else if (TILE_HOSTS.some(host => url.hostname.endsWith(host)) || (ours && url.pathname.startsWith("/lidar/"))) {
    event.respondWith(cacheFirst(request, TILES, TILE_LIMIT));
  } else if (url.hostname === "unpkg.com" || (ours && url.pathname.startsWith("/static/"))) {
    event.respondWith(cacheFirst(request, SHELL));
  } else if (ours && (url.pathname === "/api/map" || url.pathname === "/api/list")) {
    event.respondWith(view(request));
  } else if (ours && url.pathname.startsWith("/api/site/")) {
    event.respondWith(place(request, event));
  } else if (ours && (url.pathname === "/api/access" || url.pathname === "/api/photos")) {
    event.respondWith(keptFirst(request, DATA, event));
  } else if (ours && url.pathname === "/api/search") {
    event.respondWith(networkFirst(request, DATA));
  } else if (ours && url.pathname.startsWith("/api/")) {
    // Status, search, exports, and the page filling the phone's copy: live only.
    event.respondWith(fetch(request).catch(offlineAnswer));
  } else if (!ours && request.destination === "image") {
    event.respondWith(photo(request));
  }
});
