/* bandobuddy's own copy of the map, kept on the phone (IndexedDB). Shared by the page, which fills it,
 * and the service worker, which answers from it when there's no signal.
 *
 * - the index: every place in brief (name, where, what it was, how strong the evidence is), so the map,
 *   the list and search work offline anywhere in the UK, not just where you've been
 * - the details: everything about the places in the areas you've looked around (the evidence, what each
 *   source says, the ways in, the links), so a place opens in full with no signal
 *
 * With no signal the app's own questions (/api/map, /api/list, /api/site/...) are answered here the
 * same way the server answers them (store.py): same filters, same clusters, same order.
 */
"use strict";

self.LocalDB = (() => {
  const NAME = "bandobuddy";
  const MAP_SITE_LIMIT = 400;   // store.py: more than this in view and the map shows clusters
  const CLUSTER_PX = 64;        // store.py: how wide a cluster cell is on screen
  const LIST_LIMIT = 100;       // webapp.py
  const EARTH_RADIUS_M = 6371008.8;
  const HELD_SQUARES = 60;      // 1° squares of the index held in memory between questions
  const LISTED = ["key", "name", "lat", "lng", "score", "strength", "category", "condition", "kind", "sources",
                  "added", "aliases", "entrance_count"];

  let opening = null;
  const held = new Map();       // "lat,lng" -> rows, for the index version in `heldFor`
  let heldFor = null;

  function open() {
    if (!opening) {
      opening = new Promise((resolve, reject) => {
        const req = indexedDB.open(NAME, 1);
        req.onupgradeneeded = () => {
          for (const store of ["meta", "squares", "details"]) {
            if (!req.result.objectStoreNames.contains(store)) req.result.createObjectStore(store);
          }
        };
        req.onsuccess = () => {
          req.result.onversionchange = () => { req.result.close(); opening = null; };
          resolve(req.result);
        };
        req.onerror = () => { opening = null; reject(req.error); };
      });
    }
    return opening;
  }

  const finished = tx => new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = tx.onabort = () => reject(tx.error);
  });
  const answer = req => new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });

  async function read(store, key) {
    const db = await open();
    return answer(db.transaction(store).objectStore(store).get(key));
  }

  const squareOf = (lat, lng) => `${Math.floor(lat)},${Math.floor(lng)}`;

  // ---------- keeping ----------

  /** The index as /api/index sends it: {built, weak_below, columns, rows}. Replaces what was kept. */
  async function putIndex(data) {
    const col = Object.fromEntries(data.columns.map((c, i) => [c, i]));
    const squares = new Map();
    for (const row of data.rows) {
      const key = squareOf(row[col.lat], row[col.lng]);
      if (!squares.has(key)) squares.set(key, []);
      squares.get(key).push(row);
    }
    const db = await open();
    const tx = db.transaction(["squares", "meta"], "readwrite");
    tx.objectStore("squares").clear();
    for (const [key, rows] of squares) tx.objectStore("squares").put(rows, key);
    tx.objectStore("meta").put({ built: data.built, weak_below: data.weak_below, columns: data.columns,
                                 count: data.rows.length, squares: [...squares.keys()], at: Date.now() }, "index");
    await finished(tx);
    held.clear();
  }

  /** What the kept index is: {built, count, at, ...}, or null if there isn't one yet. */
  async function index() {
    try { return (await read("meta", "index")) || null; } catch (_) { return null; }
  }

  async function touchIndex() {   // checked with the server and still current
    const meta = await index();
    if (!meta) return;
    const db = await open();
    const tx = db.transaction("meta", "readwrite");
    tx.objectStore("meta").put({ ...meta, at: Date.now() }, "index");
    await finished(tx);
  }

  /** Full places, as /api/site and /api/details send them. */
  async function putDetails(sites) {
    if (!sites.length) return;
    const db = await open();
    const tx = db.transaction("details", "readwrite");
    const at = Date.now();
    for (const site of sites) tx.objectStore("details").put({ site, at }, site.key);
    await finished(tx);
  }

  async function detail(key) {
    try { return ((await read("details", key)) || {}).site || null; } catch (_) { return null; }
  }

  async function counts() {
    const db = await open();
    const meta = await index();
    const details = await answer(db.transaction("details").objectStore("details").count());
    return { places: meta ? meta.count : 0, built: meta ? meta.built : null, checked: meta ? meta.at : null, details };
  }

  /** True the first time it's asked about `name` on this phone, false after: for one-off tidying. */
  async function once(name) {
    const key = `once:${name}`;
    if (await read("meta", key)) return false;
    const db = await open();
    const tx = db.transaction("meta", "readwrite");
    tx.objectStore("meta").put(Date.now(), key);
    await finished(tx);
    return true;
  }

  async function clear() {
    const db = await open();
    const tx = db.transaction(["meta", "squares", "details"], "readwrite");
    for (const store of ["meta", "squares", "details"]) tx.objectStore(store).clear();
    await finished(tx);
    held.clear();
  }

  // ---------- answering, as the server would ----------

  async function rowsIn(meta, bbox) {
    if (heldFor !== meta.built) { held.clear(); heldFor = meta.built; }
    const have = new Set(meta.squares);
    const [w, s, e, n] = bbox || [-180, -90, 180, 90];
    const wanted = [];
    for (let lat = Math.floor(s); lat <= Math.floor(n); lat++) {
      for (let lng = Math.floor(w); lng <= Math.floor(e); lng++) {
        const key = `${lat},${lng}`;
        if (have.has(key)) wanted.push(key);
      }
    }
    const missing = wanted.filter(key => !held.has(key));
    if (missing.length) {
      const db = await open();
      const store = db.transaction("squares").objectStore("squares");
      const got = await Promise.all(missing.map(key => answer(store.get(key))));
      missing.forEach((key, i) => held.set(key, got[i] || []));
    }
    const rows = [];
    for (const key of wanted) {
      const square = held.get(key);
      held.delete(key);          // most recently used last
      held.set(key, square);
      for (const row of square) rows.push(row);
    }
    while (held.size > HELD_SQUARES) held.delete(held.keys().next().value);
    return rows;
  }

  function parseBbox(value) {
    if (!value) return null;
    const box = value.split(",").map(Number);
    return box.length === 4 && !box.some(Number.isNaN) ? box : null;
  }

  // store.py's _site_filter
  function filterOf(meta, params) {
    const c = Object.fromEntries(meta.columns.map((name, i) => [name, i]));
    const weak = ["1", "true", "yes"].includes((params.get("weak") || "").trim());
    const minScore = weak ? 0 : meta.weak_below;
    const bbox = parseBbox(params.get("bbox"));
    const cats = (params.get("categories") || "").split(",").filter(Boolean);
    const srcs = (params.get("sources") || "").split(",").filter(Boolean);
    const since = (params.get("added_since") || "").trim();
    const q = (params.get("q") || "").trim().slice(0, 80).toLowerCase();
    const test = row => {
      if (row[c.score] < minScore) return false;
      if (bbox) {
        const [w, s, e, n] = bbox;
        if (row[c.lat] < s || row[c.lat] > n || row[c.lng] < w || row[c.lng] > e) return false;
      }
      if (cats.length && !cats.includes(row[c.category])) return false;
      if (srcs.length && !srcs.some(src => (row[c.sources] || "").includes(src))) return false;
      if (since && !(row[c.added] && row[c.added] > since)) return false;
      if (q && !(row[c.name] || "").toLowerCase().includes(q)
          && !(row[c.aliases] || []).some(a => a.toLowerCase().includes(q))) return false;
      return true;
    };
    return { c, bbox, test };
  }

  const listed = (c, row) => Object.fromEntries(LISTED.map(name => [name, row[c[name]]]));
  const byScore = c => (a, b) => b[c.score] - a[c.score] || (a[c.key] < b[c.key] ? -1 : a[c.key] > b[c.key] ? 1 : 0);

  function pyRound(x) {   // Python's round(): halves go to the even number
    const r = Math.round(x);
    return Math.abs(x % 1) === 0.5 && r % 2 !== 0 ? r - 1 : r;
  }

  function haversine(lat1, lng1, lat2, lng2) {
    const rad = Math.PI / 180;
    const a = Math.sin((lat2 - lat1) * rad / 2) ** 2
      + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin((lng2 - lng1) * rad / 2) ** 2;
    return 2 * EARTH_RADIUS_M * Math.asin(Math.sqrt(a));
  }

  // store.py's map_view
  async function mapView(meta, params) {
    const { c, bbox, test } = filterOf(meta, params);
    if (!bbox) return null;
    const z = Math.trunc(parseFloat(params.get("zoom") || "10"));
    const zoom = Math.max(0, Math.min(22, Number.isNaN(z) ? 10 : z));
    const rows = (await rowsIn(meta, bbox)).filter(test);
    if (rows.length <= MAP_SITE_LIMIT || zoom >= 15) {
      rows.sort(byScore(c));
      return { mode: "sites", total: rows.length, sites: rows.slice(0, 2000).map(r => listed(c, r)), clusters: [] };
    }
    const [, s, , n] = bbox;
    const cellLng = 360 / (256 * 2 ** zoom) * CLUSTER_PX;
    const cellLat = cellLng * Math.cos(pyRound((s + n) / 2) * Math.PI / 180);
    const groups = new Map();
    for (const row of rows) {
      const lat = row[c.lat], lng = row[c.lng];
      const key = `${Math.trunc((lat + 90) / cellLat)},${Math.trunc((lng + 180) / cellLng)}`;
      let g = groups.get(key);
      if (!g) groups.set(key, g = { n: 0, lat: 0, lng: 0, s: lat, w: lng, nn: lat, e: lng, row, cats: new Map() });
      g.n++;
      g.lat += lat;
      g.lng += lng;
      g.s = Math.min(g.s, lat); g.nn = Math.max(g.nn, lat);
      g.w = Math.min(g.w, lng); g.e = Math.max(g.e, lng);
      g.cats.set(row[c.category], (g.cats.get(row[c.category]) || 0) + 1);
    }
    const clusters = [], sites = [];
    for (const g of groups.values()) {
      if (g.n === 1) { sites.push(listed(c, g.row)); continue; }
      let top = ["", 0];
      for (const cat of [...g.cats.keys()].sort()) if (g.cats.get(cat) > top[1]) top = [cat, g.cats.get(cat)];
      clusters.push({ lat: g.lat / g.n, lng: g.lng / g.n, count: g.n, category: top[0], bounds: [g.w, g.s, g.e, g.nn] });
    }
    return { mode: "clusters", total: rows.length, clusters, sites };
  }

  // store.py's list_sites, with webapp.py's distances
  async function list(meta, params) {
    const { c, bbox, test } = filterOf(meta, params);
    const rows = (await rowsIn(meta, bbox)).filter(test);
    const near = (params.get("near") || "").split(",").map(Number);
    const hasNear = near.length === 2 && !near.some(Number.isNaN);
    const sort = params.get("sort") || "nearest";
    const limit = Math.max(0, Math.min(500, parseInt(params.get("limit") || String(LIST_LIMIT), 10) || 0));
    if (sort === "nearest" && hasNear) {
      const [lat, lng] = near;
      const k2 = Math.cos(lat * Math.PI / 180) ** 2;
      const d2 = r => (r[c.lat] - lat) ** 2 + (r[c.lng] - lng) ** 2 * k2;
      rows.sort((a, b) => d2(a) - d2(b));
    } else if (sort === "newest") {
      const when = r => r[c.added] || r[c.first_seen] || "";
      rows.sort((a, b) => (when(a) < when(b) ? 1 : when(a) > when(b) ? -1 : 0) || byScore(c)(a, b));
    } else {
      rows.sort(byScore(c));
    }
    const sites = rows.slice(0, limit).map(r => {
      const site = { ...listed(c, r), summary: r[c.summary] || "" };
      if (hasNear) site.distance_m = Math.round(haversine(near[0], near[1], site.lat, site.lng));
      return site;
    });
    return { total: rows.length, sites };
  }

  // What the index knows about one place, shaped like /api/site, for when its details weren't kept.
  async function brief(meta, key) {
    const c = Object.fromEntries(meta.columns.map((name, i) => [name, i]));
    // Keys don't say where a place is, so look through the squares: only happens offline.
    for (const row of await rowsIn(meta, null)) {
      if (row[c.key] !== key) continue;
      return { ...listed(c, row), first_seen: row[c.first_seen] || "", reasons: row[c.summary] ? [row[c.summary]] : [],
               detail: { osm: [], wikidata: [], open: [] }, entrances: [], links: {},
               partial: "Only the basics of this place are kept on this phone." };
    }
    return null;
  }

  /** Answer one of the app's own requests from what's kept, or null if it can't be. */
  async function respond(url) {
    const { pathname, searchParams } = new URL(url);
    if (pathname.startsWith("/api/site/")) {
      const key = decodeURIComponent(pathname.slice("/api/site/".length));
      const kept = await detail(key);
      if (kept) return kept;
      const meta = await index();
      return meta ? brief(meta, key) : null;
    }
    const meta = await index();
    if (!meta) return null;
    const result = pathname === "/api/map" ? await mapView(meta, searchParams)
      : pathname === "/api/list" ? await list(meta, searchParams) : null;
    return result && { ...result, version: null, local: meta.built };
  }

  return { putIndex, index, touchIndex, putDetails, detail, counts, once, clear, respond };
})();
