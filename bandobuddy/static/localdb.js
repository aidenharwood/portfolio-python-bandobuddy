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
  const CURRENT_FOR_MS = 30 * 60e3;   // how long the server's last word on its map is trusted for
  const LISTED = ["key", "name", "lat", "lng", "score", "strength", "category", "condition", "kind", "sources",
                  "added", "aliases", "entrance_count", "reported", "reported_as", "reported_by", "report"];

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
    tx.objectStore("meta").put({ built: data.built, weak_below: data.weak_below, best: data.best || null, columns: data.columns,
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

  /** Which of these places' details aren't kept yet. */
  async function missing(keys) {
    const db = await open();
    const store = db.transaction("details").objectStore("details");
    const have = await Promise.all(keys.map(key => answer(store.count(key))));
    return keys.filter((_, i) => !have[i]);
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

  // ---------- news of saved places, worked out on the phone: nothing about them is ever sent anywhere ----------
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const day = on => {
    const m = /^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?/.exec(String(on || ""));
    return m ? [m[3] && String(Number(m[3])), m[2] && MONTHS[Number(m[2]) - 1], m[1]].filter(Boolean).join(" ") : "";
  };

  /** Particular places in brief, from the kept index, as {key: place}. Saved places know where they are, which
   *  says which square of the index to look in. */
  async function briefOf(places) {
    const meta = await index();
    if (!meta || !places.length) return {};
    const c = Object.fromEntries(meta.columns.map((name, i) => [name, i]));
    const bySquare = new Map();
    for (const p of places) {
      const sq = squareOf(p.lat, p.lng);
      if (!bySquare.has(sq)) bySquare.set(sq, new Set());
      bySquare.get(sq).add(p.key);
    }
    const db = await open();
    const store = db.transaction("squares").objectStore("squares");
    const squares = [...bySquare.keys()];
    const found = await Promise.all(squares.map(sq => answer(store.get(sq))));
    const out = {};
    squares.forEach((sq, i) => {
      for (const row of found[i] || []) if (bySquare.get(sq).has(row[c.key])) out[row[c.key]] = listed(c, row);
    });
    return out;
  }

  /** What about a place would be news: its condition, its last update, and what visitors have said. */
  function signature(place) {
    const r = place.report || {};
    return { condition: place.condition || "", reported: place.reported || "", reported_as: place.reported_as || "",
             reported_by: place.reported_by || "", visited: r.latest || "", accessible: r.accessible || 0,
             inaccessible: r.inaccessible || 0 };
  }

  /** What's changed since `was`, in a few words each: "now Demolished", "decided 2 Jan 2027". */
  function changes(was, now) {
    if (!was) return [];
    const said = [];
    if (now.condition && now.condition !== was.condition) said.push(`now ${now.condition}`);
    if (now.visited && now.visited !== was.visited) {
      const tried = now.accessible + now.inaccessible;
      said.push(`a new visitor report${tried ? `: got in ${now.accessible} of ${tried}` : ""}`);
    }
    if (now.reported && now.reported > was.reported && now.reported_by !== "reports")
      said.push(`${now.reported_as ? `${now.reported_as} ` : ""}${day(now.reported)}`);
    return said;
  }

  /** Saved places being watched for news: {notify, places: [{key, name, lat, lng, seen, told}]}. `seen` is what
   *  you last saw of a place, `told` what a notification last said, so neither repeats itself. */
  async function watchList() {
    try { return (await read("meta", "watch")) || { notify: false, places: [] }; }
    catch (_) { return { notify: false, places: [] }; }
  }

  async function putWatch(watch) {
    const db = await open();
    const tx = db.transaction("meta", "readwrite");
    tx.objectStore("meta").put(watch, "watch");
    await finished(tx);
  }

  /** Saved places with news since they were `seen` (or `told`): [{key, name, said: [...], now}]. Places the kept
   *  index doesn't have just now are left alone: a missing row isn't news. */
  async function savedNews(watch, against = "seen") {
    const places = await briefOf(watch.places);
    return watch.places.flatMap(p => {
      const place = places[p.key];
      if (!place) return [];
      const now = signature(place), said = changes(p[against], now);
      return said.length ? [{ key: p.key, name: place.name || p.name, said, now }] : [];
    });
  }

  /** What the server last said its map was built from (the page notes it with every status check). */
  async function noteServer(built) {
    if (!built) return;
    const db = await open();
    const tx = db.transaction("meta", "readwrite");
    tx.objectStore("meta").put({ built, at: Date.now() }, "server");
    await finished(tx);
  }

  /** The copy on the phone is the server's map as it stands: answer from it first, it's quicker. */
  async function current() {
    const [meta, server] = await Promise.all([index(), read("meta", "server")]);
    return !!(meta && server && meta.built === server.built && Date.now() - server.at < CURRENT_FOR_MS);
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
    const best = ["1", "true", "yes"].includes((params.get("best") || "").trim()) && meta.best;
    const minScore = best ? Math.max(weak ? 0 : meta.weak_below, best.min_score) : weak ? 0 : meta.weak_below;
    const bestConditions = best && new Set(best.conditions);
    const skipKinds = best && new Set(best.skip_kinds);
    const vagueKinds = best && new Set(best.vague_kinds || []);
    const bbox = parseBbox(params.get("bbox"));
    const cats = (params.get("categories") || "").split(",").filter(Boolean);
    const srcs = (params.get("sources") || "").split(",").filter(Boolean);
    const since = (params.get("added_since") || "").trim();
    const dateKind = (params.get("date") || "").trim();
    const dateFrom = (params.get("date_from") || "").trim(), dateTo = (params.get("date_to") || "").trim();
    const q = (params.get("q") || "").trim().slice(0, 80).toLowerCase();
    const squashed = q.replace(/[\s-]+/g, "");
    const test = row => {
      if (row[c.score] < minScore) return false;
      if (bbox) {
        const [w, s, e, n] = bbox;
        if (row[c.lat] < s || row[c.lat] > n || row[c.lng] < w || row[c.lng] > e) return false;
      }
      if (cats.length && !cats.includes(row[c.category])) return false;
      if (srcs.length && !srcs.some(src => (row[c.sources] || "").includes(src))) return false;
      if (since && !(row[c.added] && row[c.added] > since)) return false;
      if (dateKind && (dateFrom || dateTo)) {   // store.py's dates: "last update more than five years ago"
        const day = (row[c.dates] || {})[dateKind];
        if (!day || (dateFrom && day < dateFrom) || (dateTo && day >= dateTo)) return false;
      }
      if (q && ![row[c.name] || "", ...(row[c.aliases] || [])].some(n => {   // store.py's: spaces and hyphens aside
        const low = n.toLowerCase();
        return low.includes(q) || low.replace(/[\s-]+/g, "").includes(squashed);
      })) return false;
      if (best) {   // store.py's best spots (config.BEST)
        const cond = row[c.condition] || "";
        if (!bestConditions.has(cond) && !/^Closed \d{4}$/.test(cond)) return false;
        if (skipKinds.has((row[c.kind] || "").toLowerCase())) return false;
        if ((row[c.name] || "").startsWith("Unnamed ") && !best.unnamed_ok.includes(row[c.category])
            && vagueKinds.has((row[c.kind] || "").toLowerCase())) return false;
      }
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
    if (searchParams.get("best") && !(meta.best && meta.best.vague_kinds)) return null;   // older rules: ask the server
    if (searchParams.get("date") && !meta.columns.includes("dates")) return null;   // kept before dates: ask the server
    const result = pathname === "/api/map" ? await mapView(meta, searchParams)
      : pathname === "/api/list" ? await list(meta, searchParams) : null;
    return result && { ...result, version: null, built: meta.built, local: meta.built };
  }

  return { putIndex, index, touchIndex, putDetails, detail, missing, counts, once, clear, respond, noteServer, current,
           briefOf, signature, watchList, putWatch, savedNews };
})();
